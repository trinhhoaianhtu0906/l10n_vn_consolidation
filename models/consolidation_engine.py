"""Balance provider for consolidated statements.

The consolidated balance for any window ``[date_from, date_to]`` is::

    sum(members' ledger rows in the window) + E(date_to) - E(date_from - 1)

where ``E(d)`` are the elimination rows *cumulated* up to day ``d``. Building
every elimination as a cumulative, rounded position keeps the windows additive:
the B02 of a year is exactly the difference between two B01, and the opening
and closing statements always balance.
"""

from collections import defaultdict
from datetime import timedelta

from dateutil.relativedelta import relativedelta

from odoo.exceptions import UserError
from odoo.tools.translate import LazyTranslate

from odoo.addons.l10n_vn_financial_statements.models.engine import (
    LEDGER_IFRS,
    LEDGER_VAS,
    LedgerProvider,
    Row,
    is_pl_code,
    with_companies,
)

# Pseudo account codes. Formulas match codes by prefix, so none of these may
# start with another one.
_lt = LazyTranslate(__name__)

PREACQ = "HN_PREACQ"  # subsidiary profit or loss earned before acquisition
GOODWILL = "HN_GW"
NCI = "HN_NCI"
NCI_PL = "HN_PLNCI"  # NCI share of profit after tax (B02-DN/HN code 62)
EQUITY_PL = "HN_EQPL"  # share of associates' profit (B02-DN/HN code 27)

# Receivables, payables and loans eliminated between group members. Odoo puts
# the partner on every invoice line, so inventory or fixed asset lines bought
# from a group member carry it too: those must not be eliminated as balances.
INTRA_BALANCE_PREFIXES = (
    "128", "131", "136", "138", "244", "331", "332", "335", "336", "338", "341", "343", "344",
)
INTRA_REVENUE_PREFIXES = ("511", "515", "711")
INTRA_EXPENSE_PREFIXES = ("6", "8")


def _row(code, amount, partner_id=False):
    """Elimination row from a signed amount (debit positive)."""
    return Row(code, partner_id, max(amount, 0.0), max(-amount, 0.0), True)


class ConsolidationProvider:
    def __init__(self, group, posted_only=True, ledger=LEDGER_VAS):
        self.ledger = ledger
        # Read every company of the group, even those not active in the company switcher.
        group_companies = (group.parent_company_id | group.sudo().member_ids.company_id).with_env(group.env)
        self.env = with_companies(group.env, group_companies)
        group = group.with_env(self.env)
        self.group = group
        self.posted_only = posted_only
        self.currency = group.currency_id
        self.parent = group.parent_company_id
        self._elim_cache = {}
        self._warnings = {}

        company_sets = {}
        member_companies = self.env["res.company"]
        for member in group.member_ids:
            if member.company_id.currency_id != self.currency:
                raise UserError(
                    self.env._(
                        "%(company)s uses %(currency)s, not the currency of the group."
                        " Translating foreign currency statements is not supported yet.",
                        company=member.company_id.name,
                        currency=member.company_id.currency_id.name,
                    )
                )
            companies = self.env["res.company"].search([("id", "child_of", member.company_id.id)])
            company_sets[member.id] = companies
            member_companies |= companies
        parent_companies = (
            self.env["res.company"].search([("id", "child_of", self.parent.id)]) - member_companies
        )
        self.parent_ledger = LedgerProvider(self.env, parent_companies, posted_only, ledger)
        self.ledgers = {
            member.id: LedgerProvider(self.env, company_sets[member.id], posted_only, ledger)
            for member in group.member_ids
        }
        self.full_members = group.member_ids.filtered(lambda m: m.method == "full")
        self.equity_members = group.member_ids.filtered(lambda m: m.method == "equity")
        # commercial partner id -> member record (False for the parent)
        self.partner_owner = {self.parent.partner_id.commercial_partner_id.id: False}
        for member in group.member_ids:
            self.partner_owner[member.company_id.partner_id.commercial_partner_id.id] = member

    # -- engine interface ---------------------------------------------------

    def fiscal_year_start(self, day):
        return self.parent.compute_fiscalyear_dates(day)["date_from"]

    def rows(self, date_from=None, date_to=None, exclude_closing=False):
        result = list(self.parent_ledger.rows(date_from, date_to, exclude_closing))
        for member in self.full_members:
            result += self._member_rows(member, date_from, date_to, exclude_closing)
        result += self._window(date_from, date_to)
        return result

    def cash_rows(self, date_from, date_to):
        raise UserError(self.env._("The consolidated cash flow statement is not supported yet."))

    def extra_warnings(self, date_from, date_to):
        self.eliminations(date_to)
        return list(self._warnings.get(date_to, []))

    # -- members --------------------------------------------------------------

    def _member_rows(self, member, date_from, date_to, exclude_closing):
        """A subsidiary's rows, with its pre-acquisition profit moved to equity."""
        acq = member.acquisition_date
        if date_to < acq:
            return []
        ledger = self.ledgers[member.id]
        result = []
        if not date_from or date_from <= acq:
            for row in ledger.rows(date_from, acq, exclude_closing):
                code = PREACQ if is_pl_code(row.code) else row.code
                result.append(row._replace(code=code))
        start = max(date_from, acq + timedelta(days=1)) if date_from else acq + timedelta(days=1)
        if start <= date_to:
            result += ledger.rows(start, date_to, exclude_closing)
        return result

    def _post_acquisition(self, member, day):
        """(profit after tax, {equity code: movement}) between acquisition and ``day``.

        Closing entries are excluded, so moving profit into 421 is not counted
        twice. Amounts are credit-positive.
        """
        start = member.acquisition_date + timedelta(days=1)
        if day < start:
            return 0.0, {}
        profit = 0.0
        equity = defaultdict(float)
        for row in self.ledgers[member.id].rows(start, day, exclude_closing=True):
            if is_pl_code(row.code):
                profit += row.credit - row.debit
            elif row.code.startswith("4"):
                equity[row.code] += row.credit - row.debit
        return profit, equity

    def _ledger_equity(self, ledger, day):
        """{code: credit-positive amount} of equity read from ``ledger`` up to ``day``."""
        totals = defaultdict(float)
        for row in ledger.rows(None, day):
            if is_pl_code(row.code):
                totals[PREACQ] += row.credit - row.debit
            elif row.code.startswith("4"):
                totals[row.code] += row.credit - row.debit
        return totals

    def acquisition_equity(self, member):
        """{code: credit-positive amount} of a subsidiary's equity at acquisition.

        The stored snapshot is the VAS equity. Under IFRS, the IFRS adjustments
        booked up to the acquisition date change the net assets acquired, so
        their effect is added to the snapshot (IFRS 3).
        """
        acquired = self.ledgers[member.id]
        if member.snapshot_line_ids:
            totals = defaultdict(float, {line.code: line.amount for line in member.snapshot_line_ids})
            if self.ledger != LEDGER_VAS:
                vas = LedgerProvider(self.env, acquired.companies, self.posted_only, LEDGER_VAS)
                with_adjustments = self._ledger_equity(acquired, member.acquisition_date)
                without = self._ledger_equity(vas, member.acquisition_date)
                for code in set(with_adjustments) | set(without):
                    totals[code] += with_adjustments[code] - without[code]
        else:
            totals = self._ledger_equity(acquired, member.acquisition_date)
        return {code: amount for code, amount in totals.items() if self.currency.round(amount)}

    def investment_cost(self, member):
        if member.investment_cost:
            return member.investment_cost
        partner_id = member.company_id.partner_id.commercial_partner_id.id
        prefix = "221" if member.method == "full" else "222"
        rows = self.parent_ledger.rows(None, member.acquisition_date)
        return sum(r.debit - r.credit for r in rows if r.code.startswith(prefix) and r.partner_id == partner_id)

    # -- eliminations -----------------------------------------------------------

    def _window(self, date_from, date_to):
        closing = self.eliminations(date_to)
        if not date_from:
            return [row for _rule, _member, row in closing]
        opening = self.eliminations(date_from - timedelta(days=1))
        result = [row for _rule, _member, row in closing]
        result += [row._replace(debit=row.credit, credit=row.debit) for _rule, _member, row in opening]
        return result

    def eliminations(self, day):
        """[(rule, member or False, Row)] cumulated up to ``day``."""
        if day not in self._elim_cache:
            self._warnings[day] = []
            entries = []
            for member in self.full_members.filtered(lambda m: m.acquisition_date <= day):
                entries += self._investment(member, day)
                entries += self._goodwill_amortisation(member, day)
                entries += self._non_controlling_interest(member, day)
            for member in self.equity_members.filtered(lambda m: m.acquisition_date <= day):
                entries += self._equity_method(member, day)
            entries += self._intra_group_balances(day)
            entries += self._intra_group_profit_and_loss(day)
            entries += self._unrealised_profit(day)
            entries += self._manual_adjustments(day)
            self._elim_cache[day] = [
                (rule, member, row) for rule, member, row in entries if row.debit or row.credit
            ]
        return self._elim_cache[day]

    def _round(self, amount):
        return self.currency.round(amount)

    def _goodwill(self, member):
        equity = sum(self.acquisition_equity(member).values())
        return self._round(self.investment_cost(member) - member.ownership_pct / 100 * equity)

    def _investment(self, member, day):
        """Investment against equity at acquisition, goodwill, NCI at acquisition."""
        rule = _lt("Investment in subsidiary eliminated")
        share = member.ownership_pct / 100
        fy_start = self.fiscal_year_start(day)
        equity = self.acquisition_equity(member)
        entries = []
        for code, amount in equity.items():
            # Retained earnings of an earlier year now sit in the "prior years" account (4211).
            if member.acquisition_date < fy_start and code.startswith("4212"):
                code = "4211"
            entries.append((rule, member, _row(code, self._round(amount))))
        cost = self._round(self.investment_cost(member))
        if not cost:
            self._warnings[day].append(
                self.env._(
                    "No cost found for the investment in %(company)s: account 221 of the parent company has no"
                    " balance for this partner. Enter the cost on the group member.",
                    company=member.company_id.name,
                )
            )
        entries.append((rule, member, _row("221", -cost)))
        nci = self._round((1 - share) * sum(equity.values()))
        entries.append((rule, member, _row(NCI, -nci)))
        # Balance on goodwill (or bargain purchase gain) after rounding.
        goodwill = -sum(row.debit - row.credit for _r, _m, row in entries)
        if goodwill >= 0:
            entries.append((rule, member, _row(GOODWILL, goodwill)))
        else:
            entries.append((_lt("Gain on bargain purchase"), member, _row("711", goodwill)))
        return entries

    def _goodwill_amortisation(self, member, day):
        if self.ledger == LEDGER_IFRS:
            return []  # IFRS 3 / IAS 36: goodwill is tested for impairment, not amortised
        goodwill = self._goodwill(member)
        if goodwill <= 0:
            return []
        total_months = max(self.group.goodwill_years, 1) * 12
        elapsed = relativedelta(day + timedelta(days=1), member.acquisition_date + timedelta(days=1))
        months = min(elapsed.years * 12 + elapsed.months, total_months)
        amount = self._round(goodwill * months / total_months)
        rule = _lt("Goodwill amortisation")
        return [(rule, member, _row("642", amount)), (rule, member, _row(GOODWILL, -amount))]

    def _non_controlling_interest(self, member, day):
        minority = 1 - member.ownership_pct / 100
        if minority <= 0:
            return []
        profit, equity = self._post_acquisition(member, day)
        rule = _lt("Non-controlling interest")
        entries = [(rule, member, _row(NCI_PL, self._round(minority * profit)))]
        for code, amount in equity.items():
            entries.append((rule, member, _row(code, self._round(minority * amount))))
        total = sum(row.debit - row.credit for _r, _m, row in entries)
        entries.append((rule, member, _row(NCI, -total)))
        return entries

    def _equity_method(self, member, day):
        """Associates and joint ventures: carry the investment at share of net assets."""
        share = member.ownership_pct / 100
        partner_id = member.company_id.partner_id.commercial_partner_id.id
        profit, equity = self._post_acquisition(member, day)
        rule = _lt("Equity method")
        profit_share = self._round(share * profit)
        other_share = self._round(share * sum(equity.values()))
        entries = [
            (rule, member, _row("222", profit_share + other_share, partner_id)),
            (rule, member, _row(EQUITY_PL, -profit_share)),
            (rule, member, _row("4211", -other_share)),
        ]
        # Dividends the group received from the associate reduce the investment
        # (above, through the associate's 421) instead of being financial income.
        dividends = 0.0
        for ledger in self._group_ledgers(day):
            start = member.acquisition_date + timedelta(days=1)
            for row in ledger.rows(start, day, exclude_closing=True):
                if row.code.startswith("515") and row.partner_id == partner_id:
                    dividends += row.credit - row.debit
        dividends = self._round(dividends)
        if dividends:
            entries += [
                (_lt("Dividends from associate"), member, _row("515", dividends)),
                (_lt("Dividends from associate"), member, _row("4211", -dividends)),
            ]
        return entries

    def _group_ledgers(self, day, with_members=False):
        """Ledgers of the parent and the subsidiaries already acquired at ``day``."""
        ledgers = [(False, self.parent_ledger)]
        for member in self.full_members:
            if member.acquisition_date <= day:
                ledgers.append((member, self.ledgers[member.id]))
        return ledgers if with_members else [ledger for _m, ledger in ledgers]

    def _intra_owner(self, partner_id, owner, day):
        """The group member behind ``partner_id``, if it is not ``owner`` itself."""
        if partner_id not in self.partner_owner:
            return None
        other = self.partner_owner[partner_id]
        if other == owner:
            return None
        if other and (other.method != "full" or other.acquisition_date > day):
            return None
        return other

    def _intra_group_balances(self, day):
        rule = _lt("Intra-group balances eliminated")
        entries = []
        by_pair = defaultdict(float)
        for owner, ledger in self._group_ledgers(day, with_members=True):
            for row in ledger.rows(None, day):
                if not row.code.startswith(INTRA_BALANCE_PREFIXES):
                    continue
                other = self._intra_owner(row.partner_id, owner, day)
                if other is None:
                    continue
                balance = row.debit - row.credit
                entries.append((rule, owner, _row(row.code, -balance, row.partner_id)))
                pair = tuple(sorted([owner.id if owner else 0, other.id if other else 0]))
                by_pair[pair] += balance
        difference = self._round(sum(by_pair.values()))
        if difference:
            code = "13881" if difference > 0 else "33881"
            entries.append((_lt("Intra-group balance difference"), False, _row(code, difference)))
        for pair, balance in by_pair.items():
            if self._round(balance):
                names = [self._member_name(member_id) for member_id in pair]
                self._warnings[day].append(
                    self.env._(
                        "Intra-group balances between %(first)s and %(second)s differ by %(difference)s"
                        " at %(date)s; the difference is held in 1388/3388.",
                        first=names[0],
                        second=names[1],
                        difference=f"{self._round(balance):,.0f}",
                        date=f"{day:%d/%m/%Y}",
                    )
                )
        return entries

    def _member_name(self, member_id):
        if not member_id:
            return self.parent.name
        return self.env["vn.consolidation.member"].browse(member_id).company_id.name

    def _intra_group_profit_and_loss(self, day):
        """Intra-group revenue against the buyer's expenses; the rest is resold goods."""
        rule = _lt("Intra-group revenue and expenses eliminated")
        joined = {False: None}
        for member in self.full_members:
            joined[member] = member.acquisition_date
        revenue = defaultdict(lambda: defaultdict(float))  # (seller, buyer) -> code -> amount
        expense = defaultdict(lambda: defaultdict(float))
        for owner, ledger in self._group_ledgers(day, with_members=True):
            for other in joined:
                if other == owner or (other and other.acquisition_date > day):
                    continue
                start = max(filter(None, [joined[owner], joined[other]]), default=None)
                start = start + timedelta(days=1) if start else None
                partner = other.company_id.partner_id if other else self.parent.partner_id
                partner_id = partner.commercial_partner_id.id
                for row in ledger.rows(start, day, exclude_closing=True):
                    if row.partner_id != partner_id or row.code.startswith("5117"):
                        continue
                    if row.code.startswith(INTRA_REVENUE_PREFIXES):
                        revenue[(owner, other)][row.code] += row.credit - row.debit
                    elif row.code.startswith(INTRA_EXPENSE_PREFIXES) and not row.code.startswith("632"):
                        expense[(other, owner)][row.code] += row.debit - row.credit
        entries = []
        for pair in set(revenue) | set(expense):
            seller = pair[0]
            goods = finance = 0.0
            for code, amount in revenue[pair].items():
                amount = self._round(amount)
                entries.append((rule, seller, _row(code, amount)))
                if code.startswith("515"):
                    finance += amount
                else:
                    goods += amount
            for code, amount in expense[pair].items():
                amount = self._round(amount)
                entries.append((rule, seller, _row(code, -amount)))
                if code.startswith("635"):
                    finance -= amount
                else:
                    goods -= amount
            if goods:
                # Goods sold within the group and resold: cost of sales was counted twice.
                entries.append((rule, seller, _row("632", -goods)))
            if finance:
                # Financial income left after interest: dividends from a subsidiary.
                entries.append((_lt("Intra-group dividends eliminated"), seller, _row("4211", -finance)))
        return entries

    def _unrealised_profit(self, day):
        entries = []
        for record in self.group.unrealized_ids.filtered(lambda r: r.date == day):
            amount = self._round(record.amount)
            if not amount:
                continue
            tax = self._round(amount * record.tax_rate / 100)
            rule = _lt("Unrealised profit")
            if record.kind == "inventory":
                entries += [(rule, False, _row("632", amount)), (rule, False, _row("156", -amount))]
            else:
                entries += [(rule, False, _row("711", amount)), (rule, False, _row("211", -amount))]
            entries += [(rule, False, _row("243", tax)), (rule, False, _row("8212", -tax))]
            seller = self.group.member_ids.filtered(
                lambda m, record=record: m.company_id == record.seller_company_id and m.method == "full"
            )
            if seller and seller.ownership_pct < 100:
                share = self._round((1 - seller.ownership_pct / 100) * (amount - tax))
                entries += [
                    (_lt("Unrealised profit - non-controlling interest"), seller, _row(NCI, share)),
                    (_lt("Unrealised profit - non-controlling interest"), seller, _row(NCI_PL, -share)),
                ]
        return entries

    def _manual_adjustments(self, day):
        entries = []
        for adjustment in self.group.adjustment_ids.filtered(lambda a: a.date <= day and a.state == "confirmed"):
            for line in adjustment.line_ids:
                partner_id = line.partner_id.commercial_partner_id.id or False
                entries.append(
                    (adjustment.name, False, Row(line.code, partner_id, line.debit, line.credit, True))
                )
        return entries
