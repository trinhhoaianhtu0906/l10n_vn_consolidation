from datetime import date

from odoo.tests import TransactionCase, tagged

from odoo.addons.l10n_vn_financial_statements.models.engine import evaluate_report

from ..models.consolidation_engine import EQUITY_PL, GOODWILL, NCI, NCI_PL, PREACQ

YEAR_START = date(2026, 1, 1)
YEAR_END = date(2026, 12, 31)
ACQUISITION = date(2025, 12, 31)


@tagged("post_install", "-at_install")
class TestConsolidation(TransactionCase):
    """Parent P buys 80% of S on 31/12/2025.

    S at acquisition: capital 1,000 + unclosed profit 200 = equity 1,200.
    Cost 1,100 -> goodwill 1,100 - 80% x 1,200 = 140 (10 years: 14 a year),
    NCI at acquisition 20% x 1,200 = 240.

    2026: P buys stock 500, sells 300 to S (cost 180); S resells it all for 450,
    pays P 100 of 300 and spends 50. S profit 100, P profit 120, group profit
    450 - 180 - 50 - 14 = 206, of which NCI 20.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.parent = cls._new_company("Công ty mẹ P")
        cls.sub = cls._new_company("Công ty con S")
        cls.outsider = cls.env["res.partner"].create({"name": "Khách ngoài tập đoàn"})
        cls.p_partner = cls.parent.partner_id
        cls.s_partner = cls.sub.partner_id

        cls._post(cls.parent, date(2025, 1, 1), [("112", 5000), ("41111", -5000)])
        cls._post(cls.sub, date(2025, 6, 1), [("112", 1000), ("41111", -1000)])
        cls._post(cls.sub, date(2025, 7, 1), [("112", 200), ("511", -200, cls.outsider)])
        cls._post(cls.parent, ACQUISITION, [("221", 1100, cls.s_partner), ("112", -1100)])

        cls._post(cls.parent, date(2026, 1, 2), [("156", 500), ("112", -500)])
        cls._post(cls.parent, date(2026, 2, 1), [("1311", 300, cls.s_partner), ("511", -300, cls.s_partner)])
        cls._post(cls.parent, date(2026, 2, 1), [("632", 180), ("156", -180)])
        # Like a vendor bill: the stock line carries the partner too.
        cls._post(cls.sub, date(2026, 2, 1), [("156", 300, cls.p_partner), ("3311", -300, cls.p_partner)])
        cls._post(cls.sub, date(2026, 3, 1), [("112", 450), ("511", -450, cls.outsider)])
        cls._post(cls.sub, date(2026, 3, 1), [("632", 300), ("156", -300)])
        cls._post(cls.sub, date(2026, 4, 1), [("3311", 100, cls.p_partner), ("112", -100)])
        cls._post(cls.parent, date(2026, 4, 1), [("112", 100), ("1311", -100, cls.s_partner)])
        cls._post(cls.sub, date(2026, 5, 1), [("6421", 50), ("112", -50)])

        cls.group = cls.env["vn.consolidation.group"].create(
            {
                "name": "Tập đoàn P",
                "parent_company_id": cls.parent.id,
                "member_ids": [
                    (
                        0,
                        0,
                        {
                            "company_id": cls.sub.id,
                            "method": "full",
                            "ownership_pct": 80,
                            "acquisition_date": ACQUISITION,
                        },
                    )
                ],
            }
        )

    @classmethod
    def _new_company(cls, name):
        company = cls.env["res.company"].create(
            {"name": name, "country_id": cls.env.ref("base.vn").id, "currency_id": cls.env.ref("base.VND").id}
        )
        cls.env.user.company_ids |= company
        cls.env["account.chart.template"].with_context(allowed_company_ids=[company.id]).try_loading(
            "vn", company=company, install_demo=False
        )
        return company

    @classmethod
    def _post(cls, company, day, items):
        env = cls.env(context=dict(cls.env.context, allowed_company_ids=[company.id]))
        journal = env["account.journal"].search([("company_id", "=", company.id), ("type", "=", "general")], limit=1)
        lines = []
        for item in items:
            code, amount = item[0], item[1]
            partner = item[2] if len(item) > 2 else env["res.partner"]
            # l10n_vn pads codes to 4 digits: 112 is stored as 1120.
            Account = env["account.account"]
            account = Account.search([("code", "=", code), ("company_ids", "in", company.id)], limit=1)
            account = account or Account.search(
                [("code", "=like", code + "%"), ("company_ids", "in", company.id)], order="code", limit=1
            )
            assert account, code
            lines.append(
                (0, 0, {"account_id": account.id, "partner_id": partner.id, "name": code,
                        "debit": max(amount, 0), "credit": max(-amount, 0)})
            )
        move = env["account.move"].create({"journal_id": journal.id, "date": day, "line_ids": lines})
        move.action_post()
        return move

    def _run(self, xmlid, group=None):
        report = self.env.ref(f"l10n_vn_consolidation.{xmlid}")
        provider = (group or self.group)._get_provider()
        result = evaluate_report(report, provider, YEAR_START, YEAR_END)
        values = {item["line"].code: item["value"] for item in result["lines"]}
        previous = {item["line"].code: item["value_cmp"] for item in result["lines"]}
        return values, previous, result["warnings"]

    def test_pseudo_codes_are_not_prefixes_of_each_other(self):
        # Formulas match by prefix: NBAL("HN_NCI") must not pick up another pseudo code.
        codes = [PREACQ, GOODWILL, NCI, NCI_PL, EQUITY_PL]
        for code in codes:
            for other in codes:
                self.assertFalse(code != other and other.startswith(code), (code, other))

    def test_acquisition_snapshot(self):
        member = self.group.member_ids
        member.action_take_snapshot()
        snapshot = {line.code: line.amount for line in member.snapshot_line_ids}
        self.assertEqual(snapshot, {"41111": 1000, "HN_PREACQ": 200})

    def test_consolidated_position(self):
        values, opening, warnings = self._run("report_b01_hn")
        self.assertEqual(warnings, [])
        self.assertEqual(values["111"], 5000)
        self.assertEqual(values["141"], 320)
        self.assertEqual(values["131"], 0)
        self.assertEqual(values["311"], 0)
        self.assertEqual(values["261"], 0)
        self.assertEqual(values["279"], 126)
        self.assertEqual(values["411a"], 5000)
        self.assertEqual(values["429"], 260)
        self.assertEqual(values["420a"], 0)
        self.assertEqual(values["420b"], 186)
        self.assertEqual(values["280"], 5446)
        self.assertEqual(values["440"], 5446)
        # At acquisition: cash 3,900 + 1,200, goodwill 140; capital 5,000, NCI 240.
        self.assertEqual(opening["280"], 5240)
        self.assertEqual(opening["279"], 140)
        self.assertEqual(opening["429"], 240)
        self.assertEqual(opening["440"], 5240)

    def test_consolidated_profit_and_loss(self):
        values, previous, warnings = self._run("report_b02_hn")
        self.assertEqual(warnings, [])
        self.assertEqual(values["01"], 450)
        self.assertEqual(values["11"], 180)
        self.assertEqual(values["26"], 64)
        self.assertEqual(values["60"], 206)
        self.assertEqual(values["62"], 20)
        self.assertEqual(values["61"], 186)
        # S's 2025 profit was earned before acquisition: not group profit.
        self.assertEqual(previous["01"], 0)
        self.assertEqual(previous["60"], 0)

    def test_intra_group_dividend(self):
        """S pays a dividend of 50: 40 to P (eliminated), 10 to outside shareholders."""
        self._post(
            self.sub,
            date(2026, 6, 30),
            [("4211", 50), ("332", -40, self.p_partner), ("332", -10, self.outsider)],
        )
        self._post(self.parent, date(2026, 6, 30), [("13881", 40, self.s_partner), ("515", -40, self.s_partner)])
        b01, _o, warnings = self._run("report_b01_hn")
        b02, _p, _w = self._run("report_b02_hn")
        self.assertEqual(warnings, [])
        self.assertEqual(b02["22"], 0)
        self.assertEqual(b02["60"], 206)
        self.assertEqual(b01["313"], 10)
        self.assertEqual(b01["429"], 250)
        self.assertEqual(b01["280"], b01["440"])

    def test_unrealised_profit(self):
        self.env["vn.consolidation.unrealized"].create(
            {
                "group_id": self.group.id,
                "date": YEAR_END,
                "seller_company_id": self.parent.id,
                "buyer_company_id": self.sub.id,
                "goods_value": 100,
                "margin_pct": 30,
            }
        )
        b01, _o, _w = self._run("report_b01_hn")
        b02, _p, _w = self._run("report_b02_hn")
        self.assertEqual(b01["141"], 290)
        self.assertEqual(b01["272"], 6)
        self.assertEqual(b02["60"], 182)
        self.assertEqual(b01["280"], b01["440"])

    def test_equity_method_associate(self):
        associate = self._new_company("Công ty liên kết A")
        self._post(self.parent, ACQUISITION, [("222", 300, associate.partner_id), ("112", -300)])
        self._post(associate, date(2026, 8, 1), [("112", 100), ("511", -100, self.outsider)])
        self.group.member_ids = [
            (0, 0, {"company_id": associate.id, "method": "equity", "ownership_pct": 30, "acquisition_date": ACQUISITION})
        ]
        b01, _o, _w = self._run("report_b01_hn")
        b02, _p, _w = self._run("report_b02_hn")
        self.assertEqual(b02["27"], 30)
        self.assertEqual(b02["60"], 236)
        self.assertEqual(b02["01"], 450)  # the associate's revenue is not consolidated
        self.assertEqual(b01["262"], 330)
        self.assertEqual(b01["280"], b01["440"])

    def test_intra_group_mismatch_warning(self):
        self._post(self.parent, date(2026, 9, 1), [("1311", 10, self.s_partner), ("711", -10, self.s_partner)])
        b01, _o, warnings = self._run("report_b01_hn")
        self.assertTrue(any("differ by" in w for w in warnings), warnings)
        self.assertEqual(b01["280"], b01["440"])

    def test_member_companies_inactive_in_switcher(self):
        """An accountant with only the parent active in the UI still gets the whole group."""
        user = self.env["res.users"].create({
            "name": "Kế toán tập đoàn",
            "login": "ke_toan_tap_doan",
            "company_id": self.parent.id,
            "company_ids": [(6, 0, [self.parent.id, self.sub.id])],
            "group_ids": [(6, 0, [self.env.ref("account.group_account_manager").id])],
        })
        env = self.env(user=user, context={"allowed_company_ids": [self.parent.id]})
        data = env["vn.report.wizard"].get_dynamic_report({
            "report_id": self.env.ref("l10n_vn_consolidation.report_b02_hn").id,
            "consolidation_group_id": self.group.id,
            "date_from": "2026-01-01",
            "date_to": "2026-12-31",
            "target_move": "posted",
        })
        values = {line["code"]: line["value"] for line in data["lines"]}
        self.assertEqual(values["60"], 206)
        self.assertEqual(values["62"], 20)
        self.assertEqual(data["warnings"], [])
        line_01 = next(line for line in data["lines"] if line["code"] == "01")
        drill = env["vn.report.wizard.line"].browse(line_01["id"]).get_drilldown()
        self.assertEqual(drill["inactive_companies"], [self.sub.name])
        self.assertEqual(drill["action"]["res_model"], "account.move.line")

    def test_wizard_and_worksheet(self):
        wizard = self.env["vn.report.wizard"].create(
            {
                "consolidation_group_id": self.group.id,
                "report_id": self.env.ref("l10n_vn_consolidation.report_b01_hn").id,
                "company_id": self.parent.id,
                "date_from": YEAR_START,
                "date_to": YEAR_END,
            }
        )
        wizard.action_compute()
        self.assertEqual(wizard.line_ids.filtered(lambda line: line.code == "429").value, 260)
        worksheet = self.env["vn.consolidation.worksheet"].create({"group_id": self.group.id, "date": YEAR_END})
        worksheet.action_compute()
        self.assertTrue(worksheet.line_ids)
        self.assertEqual(sum(worksheet.line_ids.mapped("debit")), sum(worksheet.line_ids.mapped("credit")))
