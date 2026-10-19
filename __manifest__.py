{  # noqa: B018
    "name": "Vietnam - Consolidated Financial Statements (Circular 202/2014 and 43/2026)",
    "version": "19.0.1.0.0",
    "summary": "Consolidated financial statements B01-DN/HN, B02-DN/HN with computed eliminations",
    "description": """
Consolidation of several Odoo companies of one database into the consolidated
statements on the forms of Circular 99/2025 (B01-DN/HN, B02-DN/HN), following Circular 202/2014/TT-BTC as
amended by Circular 43/2026/TT-BTC.

Eliminations are computed, never posted to the members' ledgers:

- investment in subsidiaries against equity at acquisition, goodwill or gain
  on bargain purchase, non-controlling interest at acquisition;
- goodwill amortisation (straight line, at most 10 years);
- non-controlling interest in post-acquisition profit and equity movements;
- intra-group balances, revenue, expenses and dividends;
- unrealised profit in inventory and fixed assets, with deferred tax;
- associates and joint ventures under the equity method;
- manual adjustments.
""",
    "category": "Accounting/Localizations/Reporting",
    "author": "Tricasia",
    "license": "LGPL-3",
    "depends": ["l10n_vn_financial_statements"],
    "data": [
        "security/ir.model.access.csv",
        "data/vn_report_hn.xml",
        "views/vn_consolidation_views.xml",
        "wizard/vn_report_wizard_views.xml",
        "wizard/vn_consolidation_worksheet_views.xml",
        "views/menus.xml",
    ],
    "installable": True,
}
