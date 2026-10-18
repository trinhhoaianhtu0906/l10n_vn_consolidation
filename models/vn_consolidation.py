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
        "res.company", string="Công ty mẹ", required=True, default=lambda self: self.env.company
    )
    currency_id = fields.Many2one(related="parent_company_id.currency_id")
    member_ids = fields.One2many("vn.consolidation.member", "group_id", string="Thành viên")
    goodwill_years = fields.Integer(
        string="Số năm phân bổ lợi thế thương mại",
        default=10,
        help="Phân bổ đều, tối đa 10 năm theo Thông tư 202/2014/TT-BTC.",
    )
    cit_rate = fields.Float(string="Thuế suất TNDN (%)", default=20.0)
    unrealized_ids = fields.One2many("vn.consolidation.unrealized", "group_id", string="Lãi chưa thực hiện")
    adjustment_ids = fields.One2many("vn.consolidation.adjustment", "group_id", string="Bút toán điều chỉnh")
    note = fields.Html()

    @api.constrains("goodwill_years")
    def _check_goodwill_years(self):
        for group in self:
            if not 1 <= group.goodwill_years <= 10:
                raise ValidationError(self.env._("Thời gian phân bổ lợi thế thương mại từ 1 đến 10 năm."))

    @api.constrains("parent_company_id", "member_ids")
    def _check_members(self):
        for group in self:
            companies = group.member_ids.company_id
            if len(companies) != len(group.member_ids):
                raise ValidationError(self.env._("Một công ty chỉ được khai báo một lần trong nhóm."))
            if group.parent_company_id in companies:
                raise ValidationError(self.env._("Công ty mẹ không được khai báo là thành viên."))

    def _get_provider(self, posted_only=True):
        self.ensure_one()
        return ConsolidationProvider(self, posted_only=posted_only)

    def action_open_worksheet(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Bảng tính loại trừ"),
            "res_model": "vn.consolidation.worksheet",
            "view_mode": "form",
            "target": "new",
            "context": {"default_group_id": self.id},
        }

    def action_open_report(self):
        self.ensure_one()
        return {
            "type": "ir.actions.client",
            "name": self.env._("Báo cáo hợp nhất"),
            "tag": "l10n_vn_tt99_reports.dynamic_report",
            "context": {
                "default_consolidation_group_id": self.id,
                "default_report_id": self.env.ref("l10n_vn_tt99_consolidation.report_b01_hn").id,
            },
        }


class VnConsolidationMember(models.Model):
    _name = "vn.consolidation.member"
    _description = "Consolidation group member"
    _order = "group_id, method, company_id"

    group_id = fields.Many2one("vn.consolidation.group", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one("res.company", string="Công ty", required=True)
    currency_id = fields.Many2one(related="group_id.currency_id")
    method = fields.Selection(
        [
            ("full", "Công ty con (hợp nhất toàn bộ)"),
            ("equity", "Liên doanh, liên kết (vốn chủ sở hữu)"),
        ],
        string="Phương pháp",
        required=True,
        default="full",
    )
    ownership_pct = fields.Float(
        string="Tỷ lệ lợi ích (%)",
        required=True,
        default=100.0,
        help="Tỷ lệ lợi ích của công ty mẹ, kể cả gián tiếp.",
    )
    acquisition_date = fields.Date(string="Ngày nắm quyền kiểm soát / ảnh hưởng", required=True)
    investment_cost = fields.Monetary(
        string="Giá gốc khoản đầu tư",
        help="Để trống: lấy số dư TK 221 (hoặc 222) của công ty mẹ, chi tiết theo đối tượng là công ty này,"
        " tại ngày mua.",
    )
    snapshot_line_ids = fields.One2many(
        "vn.consolidation.snapshot.line",
        "member_id",
        string="Vốn chủ sở hữu tại ngày mua",
        help="Để trống: tự lấy từ sổ của công ty con tại ngày mua.",
    )

    _pct_check = models.Constraint(
        "CHECK(ownership_pct > 0 AND ownership_pct <= 100)",
        "Tỷ lệ lợi ích phải lớn hơn 0 và không quá 100%.",
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
        string="Tài khoản",
        required=True,
        help="Mã tài khoản vốn chủ sở hữu (4xxx). HN_PREACQ: lãi lỗ trước ngày mua chưa kết chuyển.",
    )
    amount = fields.Monetary(string="Số dư Có", required=True)


class VnConsolidationUnrealized(models.Model):
    _name = "vn.consolidation.unrealized"
    _description = "Unrealised intra-group profit at a reporting date"
    _order = "date desc, id"

    group_id = fields.Many2one("vn.consolidation.group", required=True, ondelete="cascade", index=True)
    currency_id = fields.Many2one(related="group_id.currency_id")
    date = fields.Date(string="Tại ngày", required=True, help="Ngày lập báo cáo hợp nhất (cuối kỳ).")
    seller_company_id = fields.Many2one("res.company", string="Bên bán", required=True)
    buyer_company_id = fields.Many2one("res.company", string="Bên mua", required=True)
    kind = fields.Selection(
        [("inventory", "Hàng tồn kho"), ("fixed_asset", "Tài sản cố định")],
        string="Loại tài sản",
        required=True,
        default="inventory",
    )
    goods_value = fields.Monetary(
        string="Giá trị tài sản nội bộ còn lại", help="Giá trị theo giá mua của bên mua, còn tồn tại ngày lập báo cáo."
    )
    margin_pct = fields.Float(string="Tỷ suất lãi gộp bên bán (%)")
    amount = fields.Monetary(
        string="Lãi chưa thực hiện",
        compute="_compute_amount",
        store=True,
        readonly=False,
        help="Tự tính = giá trị còn lại x tỷ suất lãi gộp; có thể sửa tay.",
    )
    tax_rate = fields.Float(
        string="Thuế suất TNDN (%)",
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
    name = fields.Char(string="Diễn giải", required=True)
    date = fields.Date(string="Ngày hiệu lực", required=True, help="Áp dụng cho mọi báo cáo lập từ ngày này trở đi.")
    state = fields.Selection(
        [("draft", "Nháp"), ("confirmed", "Áp dụng")], default="draft", required=True, string="Trạng thái"
    )
    line_ids = fields.One2many("vn.consolidation.adjustment.line", "adjustment_id", string="Dòng")

    def action_confirm(self):
        for adjustment in self:
            debit = sum(adjustment.line_ids.mapped("debit"))
            credit = sum(adjustment.line_ids.mapped("credit"))
            if adjustment.group_id.currency_id.compare_amounts(debit, credit):
                raise ValidationError(self.env._("Bút toán điều chỉnh %s chưa cân Nợ - Có.", adjustment.name))
        self.state = "confirmed"

    def action_draft(self):
        self.state = "draft"


class VnConsolidationAdjustmentLine(models.Model):
    _name = "vn.consolidation.adjustment.line"
    _description = "Manual consolidation adjustment line"

    adjustment_id = fields.Many2one("vn.consolidation.adjustment", required=True, ondelete="cascade", index=True)
    currency_id = fields.Many2one(related="adjustment_id.group_id.currency_id")
    code = fields.Char(string="Tài khoản", required=True, help="Mã tài khoản, hoặc HN_GW, HN_NCI, HN_PLNCI, HN_EQPL.")
    partner_id = fields.Many2one("res.partner", string="Đối tượng")
    debit = fields.Monetary(string="Nợ")
    credit = fields.Monetary(string="Có")
