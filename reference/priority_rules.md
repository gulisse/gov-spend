# Priority Rules for Ambigous Procurement Data
When an aggregate spend description could map to multiple taxonomy categories, apply these rules in strict hierarchical order (1 to 6). Stop at the first rule that resolves the ambiguity. Do not process lower-priority rules once a match is found. Return the code from the taxonomy.

## RULE 1 — PURPOSE OVER METHOD (Core Directive)
Classify by the council service or operational objective the spend supports, not the raw trade or physical activity performed.
* Ask: "What specific council service or department does this money ultimately serve?"
* Example: "Repairing a sports hall roof" → Map to Arts & Leisure Services (Sport & Fitness), NOT Building Construction Materials.

## RULE 2 — LEVERAGE FINANCIAL METRICS & SCALE (Scale Directive)
Use the companion financial metrics (Total Spent, Max, Min, Avg, Median) to determine the nature and scope of the engagement.
* Sub-Rule 2A (Capital Works vs. Minor Purchase): If Total Spent or Max value is extremely high (e.g., enterprise-scale/capital spend thresholds) paired with a low row count, bias toward "Works - Construction, Repair & Maintenance" rather than standalone commodities, though NOT necessarily.
* Sub-Rule 2B (Subscription/Retainer vs. One-off): High row counts with tightly consistent Min/Max/Median metrics point to structured service contracts, framework agreements, or repeating utility charges rather than ad-hoc product procurement.

## RULE 3 — COMMODITY VS. SERVICE DELIVERY
Material/trade categories (e.g., Building Construction Materials) apply ONLY when the council is buying raw goods, supplies, or commodities as a standalone itemized purchase. When materials are bundled within a delivered or contracted service, classify under the service purpose.
* Example: "50 tonnes of road aggregate" → Map to Highways Materials (Standalone Commodity).
* Example: "Resurfacing Elm Street" → Map to Works - Construction, Repair & Maintenance -> Roads (Service Delivery).

## RULE 4 — SPECIFIC OVER GENERAL
If one candidate category is a narrow, explicit match and the other is a broad catch-all or "Not Elsewhere Classified" branch, prioritize the narrow category.
* Example: "Purchase of library self-service kiosks" → Map to Libraries, NOT IT Equipment & Supplies or Not Elsewhere Classified.

## RULE 5 — DEPARTMENT / FACILITY SIGNALS
If the description explicitly names a specific building, asset type, client demographic, or council department, treat that text as a strong service-area anchor.
* Example: "Boiler maintenance — Oakfield Care Home" → Map to Adult Social Care, NOT Building Construction Materials (Heating & Ventilation).

## RULE 6 — DEFAULT TO PHYSICAL/OPERATIONAL & FLAG
If rules 1 through 5 fail to definitively break the tie, default classification to the physical works or raw operational asset category, and append the flag "[AMBIGUOUS]" to the end of the response object.