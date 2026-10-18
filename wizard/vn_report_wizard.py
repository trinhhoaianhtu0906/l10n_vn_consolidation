from odoo import api, fields, models
from odoo.exceptions import UserError

from odoo.addons.l10n_vn_tt99_reports.models.engine import with_companies


class VnReportWizard(models.TransientModel):
    _inherit = "vn.report.wizard"

    report_id = fields.Many2one(domain="[('consolidated', '=', consolidation_group_id != False)]")
    consolidation_group_id = fields.Many2one("vn.consolidation.group", string="Nhóm hợp nhất")

    @api.onchange("consolidation_group_id")
    def _onchange_consolidation_group_id(self):
        if self.consolidation_group_id:
            self.company_id = self.consolidation_group_id.parent_company_id
        if self.report_id and self.report_id.consolidated != bool(self.consolidation_group_id):
            self.report_id = False

    def _get_provider(self):
        if self.report_id.consolidated:
            if not self.consolidation_group_id:
                raise UserError(self.env._("Báo cáo hợp nhất cần chọn nhóm hợp nhất."))
            return self.consolidation_group_id._get_provider(posted_only=self.target_move == "posted")
        return super()._get_provider()

    @api.model
    def _dynamic_groups(self):
        return self.env["vn.consolidation.group"].search_read([], ["name", "parent_company_id"])

    def _drilldown_companies(self):
        if not (self.report_id.consolidated and self.consolidation_group_id):
            return super()._drilldown_companies()
        group = self.consolidation_group_id
        roots = group.parent_company_id | group.sudo().member_ids.filtered(lambda m: m.method == "full").company_id
        env = with_companies(self.env, roots.with_env(self.env))
        return env["res.company"].search([("id", "child_of", roots.ids)])

    def _compute_report(self):
        if self.consolidation_group_id and self.report_id.consolidated:
            self.company_id = self.consolidation_group_id.parent_company_id
        return super()._compute_report()
