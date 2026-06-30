# Business Classification Rules

Apply these rules when assigning a supplier to a BUSINESS_SUBTYPE. They cover
only the cases the model cannot infer from the taxonomy alone; keep judgement
lean otherwise.

1. **Classify by core business, not the transaction.** Assign the sub-type that
   reflects what the supplier fundamentally *is or does*, not what any single
   payment was for.

2. **Vehicle services sit under Passenger Transport.** Vehicle repair,
   maintenance, hire, leasing, fuel, recovery and fleet management all belong to
   the Passenger Transport type — not a separate motor/vehicle category.

3. **Hotels and accommodation sit under Housing & Accommodation.** Hotels, B&Bs
   and temporary or emergency accommodation map to Housing & Accommodation, not
   hospitality or catering.

4. **Use context as the tie-breaker.** When the supplier name is ambiguous, let
   the service area, department and supplier category context decide the type.

5. **Escape hatch.** If the correct sub-type is unclear, choose the
   "Other <Type>" sub-type of the best-fitting type. If even the type is
   unclear, choose "Other / Unclassified". Never force a specific sub-type you
   are not reasonably confident in.
