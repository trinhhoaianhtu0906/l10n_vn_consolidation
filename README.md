# Vietnam - Consolidated Financial Statements (TT202/TT43, TT99 forms)

Odoo 19 module by Tricasia: B01-DN/HN and B02-DN/HN for several companies of one database. Eliminations (investment vs equity, goodwill, non-controlling interest, intra-group balances and transactions, dividends, unrealised profit, equity method, manual adjustments) are computed on the fly, never posted to the members' ledgers.

- Elimination logic: `models/consolidation_engine.py`
- Documentation: `docs/tt99/TT99_Documentation.pdf` in [Tricasia/odoo-dev](https://github.com/Tricasia/odoo-dev), sections 8 and 9.

Tests: `odoo -d <tmp_db> -i l10n_vn_tt99_consolidation --test-tags /l10n_vn_tt99_consolidation --stop-after-init`
