from odoo import api, fields, models
from odoo.exceptions import ValidationError

from .consolidation_engine import ConsolidationProvider


class VnConsolidationGroup(models.Model):
    _name = "vn.consolidation.group"
    _description = "Consolidation group"
    _order = "name"

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    parent_company_id = fields.Many2one(
        "res.company", string="Parent Company", required=True, default=lambda self: self.env.company
    )
    currency_id = fields.Many2one(related="parent_company_id.currency_id")
    member_ids = fields.One2many("vn.consolidation.member", "group_id", string="Members")
    goodwill_years = fields.Integer(
        string="Goodwill Amortisation (Years)",
        default=10,
        help="Straight line, at most 10 years under Circular 202/2014/TT-BTC.",
    )
    cit_rate = fields.Float(string="Corporate Income Tax Rate (%)", default=20.0)
    unrealized_ids = fields.One2many("vn.consolidation.unrealized", "group_id", string="Unrealised Profit")
    adjustment_ids = fields.One2many("vn.consolidation.adjustment", "group_id", string="Adjustments")
    note = fields.Html()

    @api.constrains("goodwill_years")
    def _check_goodwill_years(self):
        for group in self:
            if not 1 <= group.goodwill_years <= 10:
                raise ValidationError(self.env._("Goodwill is amortised over 1 to 10 years."))

    @api.constrains("parent_company_id", "member_ids")
    def _check_members(self):
        for group in self:
            companies = group.member_ids.company_id
            if len(companies) != len(group.member_ids):
                raise ValidationError(self.env._("A company can only be a member of a group once."))
            if group.parent_company_id in companies:
                raise ValidationError(self.env._("The parent company cannot also be a member."))

    def _get_provider(self, posted_only=True):
        self.ensure_one()
        return ConsolidationProvider(self, posted_only=posted_only)

    def action_open_worksheet(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Elimination Worksheet"),
            "res_model": "vn.consolidation.worksheet",
            "view_mode": "form",
            "target": "new",
            "context": {"default_group_id": self.id},
        }

    def action_open_report(self):
        self.ensure_one()
        return {
            "type": "ir.actions.client",
            "name": self.env._("Consolidated Statements"),
            "tag": "l10n_vn_financial_statements.dynamic_report",
            "context": {
                "default_consolidation_group_id": self.id,
                "default_report_id": self.env.ref("l10n_vn_consolidation.report_b01_hn").id,
            },
        }


class VnConsolidationMember(models.Model):
    _name = "vn.consolidation.member"
    _description = "Consolidation group member"
    _order = "group_id, method, company_id"

    group_id = fields.Many2one("vn.consolidation.group", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one("res.company", string="Company", required=True)
    currency_id = fields.Many2one(related="group_id.currency_id")
    method = fields.Selection(
        [
            ("full", "Subsidiary (full consolidation)"),
            ("equity", "Associate or joint venture (equity method)"),
        ],
        string="Method",
        required=True,
        default="full",
    )
    ownership_pct = fields.Float(
        string="Ownership (%)",
        required=True,
        default=100.0,
        help="Interest of the parent company, direct and indirect.",
    )
    acquisition_date = fields.Date(string="Acquisition Date", required=True, help="Date control or significant influence was obtained.")
    investment_cost = fields.Monetary(
        string="Investment Cost",
        help="Empty: the balance of account 221 (or 222) of the parent company for this company as partner,"
        " at the acquisition date.",
    )
    snapshot_line_ids = fields.One2many(
        "vn.consolidation.snapshot.line",
        "member_id",
        string="Equity at Acquisition",
        help="Empty: read from the subsidiary's ledger at the acquisition date.",
    )

    _pct_check = models.Constraint(
        "CHECK(ownership_pct > 0 AND ownership_pct <= 100)",
        "Ownership must be above 0 and at most 100%.",
    )

    def action_take_snapshot(self):
        """Store the equity at acquisition so it can be reviewed and adjusted."""
        for member in self:
            provider = member.group_id._get_provider()
            member.snapshot_line_ids = [fields.Command.clear()]
            equity = provider.acquisition_equity(member)
            member.snapshot_line_ids = [
                fields.Command.create({"code": code, "amount": amount}) for code, amount in sorted(equity.items())
            ]
        return True


class VnConsolidationSnapshotLine(models.Model):
    _name = "vn.consolidation.snapshot.line"
    _description = "Equity of a subsidiary at acquisition"
    _order = "member_id, code"

    member_id = fields.Many2one("vn.consolidation.member", required=True, ondelete="cascade", index=True)
    currency_id = fields.Many2one(related="member_id.currency_id")
    code = fields.Char(
        string="Account",
        required=True,
        help="Equity account code (4xxx). HN_PREACQ: unclosed profit or loss earned before the acquisition.",
    )
    amount = fields.Monetary(string="Credit Balance", required=True)


class VnConsolidationUnrealized(models.Model):
    _name = "vn.consolidation.unrealized"
    _description = "Unrealised intra-group profit at a reporting date"
    _order = "date desc, id"

    group_id = fields.Many2one("vn.consolidation.group", required=True, ondelete="cascade", index=True)
    currency_id = fields.Many2one(related="group_id.currency_id")
    date = fields.Date(string="Date", required=True, help="Reporting date of the consolidated statements (period end).")
    seller_company_id = fields.Many2one("res.company", string="Seller", required=True)
    buyer_company_id = fields.Many2one("res.company", string="Buyer", required=True)
    kind = fields.Selection(
        [("inventory", "Inventory"), ("fixed_asset", "Fixed Asset")],
        string="Asset Type",
        required=True,
        default="inventory",
    )
    goods_value = fields.Monetary(
        string="Remaining Intra-group Assets", help="Value at the buyer's purchase price still held at the reporting date."
    )
    margin_pct = fields.Float(string="Seller Gross Margin (%)")
    amount = fields.Monetary(
        string="Unrealised Profit",
        compute="_compute_amount",
        store=True,
        readonly=False,
        help="Remaining value x gross margin; can be edited."
    )
    tax_rate = fields.Float(
        string="Tax Rate (%)",
        compute="_compute_tax_rate",
        store=True,
        readonly=False,
    )

    @api.depends("goods_value", "margin_pct")
    def _compute_amount(self):
        for record in self:
            record.amount = record.goods_value * record.margin_pct / 100

    @api.depends("group_id.cit_rate")
    def _compute_tax_rate(self):
        for record in self:
            record.tax_rate = record.group_id.cit_rate


class VnConsolidationAdjustment(models.Model):
    _name = "vn.consolidation.adjustment"
    _description = "Manual consolidation adjustment"
    _order = "date desc, id desc"

    group_id = fields.Many2one("vn.consolidation.group", required=True, ondelete="cascade", index=True)
    name = fields.Char(string="Description", required=True)
    date = fields.Date(string="Effective Date", required=True, help="Applies to every statement at this date or later.")
    state = fields.Selection(
        [("draft", "Draft"), ("confirmed", "Applied")], default="draft", required=True, string="Status"
    )
    line_ids = fields.One2many("vn.consolidation.adjustment.line", "adjustment_id", string="Lines")

    def action_confirm(self):
        for adjustment in self:
            debit = sum(adjustment.line_ids.mapped("debit"))
            credit = sum(adjustment.line_ids.mapped("credit"))
            if adjustment.group_id.currency_id.compare_amounts(debit, credit):
                raise ValidationError(self.env._("Adjustment %s is not balanced.", adjustment.name))
        self.state = "confirmed"

    def action_draft(self):
        self.state = "draft"


class VnConsolidationAdjustmentLine(models.Model):
    _name = "vn.consolidation.adjustment.line"
    _description = "Manual consolidation adjustment line"

    adjustment_id = fields.Many2one("vn.consolidation.adjustment", required=True, ondelete="cascade", index=True)
    currency_id = fields.Many2one(related="adjustment_id.group_id.currency_id")
    code = fields.Char(string="Account", required=True, help="Account code, or HN_GW, HN_NCI, HN_PLNCI, HN_EQPL.")
    partner_id = fields.Many2one("res.partner", string="Partner")
    debit = fields.Monetary(string="Debit")
    credit = fields.Monetary(string="Credit")
