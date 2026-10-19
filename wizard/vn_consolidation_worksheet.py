from odoo import fields, models
from odoo.tools.translate import LazyGettext


class VnConsolidationWorksheet(models.TransientModel):
    _name = "vn.consolidation.worksheet"
    _description = "Consolidation elimination worksheet"

    group_id = fields.Many2one("vn.consolidation.group", string="Consolidation Group", required=True)
    currency_id = fields.Many2one(related="group_id.currency_id")
    date = fields.Date(string="Date", required=True, default=fields.Date.context_today)
    line_ids = fields.One2many("vn.consolidation.worksheet.line", "worksheet_id", string="Eliminations")
    warning_text = fields.Text(string="Warnings", readonly=True)
    computed = fields.Boolean(readonly=True)

    def action_compute(self):
        self.ensure_one()
        provider = self.group_id._get_provider()
        commands = [fields.Command.clear()]
        Partner = self.env["res.partner"]
        for rule, member, row in provider.eliminations(self.date):
            commands.append(
                fields.Command.create(
                    {
                        "rule": self.env._(rule) if isinstance(rule, LazyGettext) else rule,
                        "company_id": member.company_id.id if member else False,
                        "code": row.code,
                        "partner_id": Partner.browse(row.partner_id).id if row.partner_id else False,
                        "debit": row.debit,
                        "credit": row.credit,
                    }
                )
            )
        warnings = provider.extra_warnings(None, self.date)
        self.write({"line_ids": commands, "warning_text": "\n".join(warnings) or False, "computed": True})
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
            "name": self.env._("Elimination Worksheet"),
        }


class VnConsolidationWorksheetLine(models.TransientModel):
    _name = "vn.consolidation.worksheet.line"
    _description = "Consolidation elimination worksheet line"
    _order = "id"

    worksheet_id = fields.Many2one("vn.consolidation.worksheet", required=True, ondelete="cascade")
    currency_id = fields.Many2one(related="worksheet_id.currency_id")
    rule = fields.Char(string="Elimination", readonly=True)
    company_id = fields.Many2one("res.company", string="Member", readonly=True)
    code = fields.Char(string="Account", readonly=True)
    partner_id = fields.Many2one("res.partner", string="Partner", readonly=True)
    debit = fields.Monetary(string="Debit", readonly=True)
    credit = fields.Monetary(string="Credit", readonly=True)
