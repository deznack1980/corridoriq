/* ==========================================================================
   SAMPLE DATA — DEMONSTRATION ONLY
   --------------------------------------------------------------------------
   Fictional contractor, suppliers, project, bill of materials, RFQs, and
   quotes for the procurement preview screens. Nothing here comes from, or is
   written to, the CorridorIQ database or API. Supplier names are neutral
   placeholders; prices are illustrative and are not any real business's
   pricing, inventory, or quote. Production code must never load this file
   (enforced by pipeline/tests/test_option_b_safety.py).
   ========================================================================== */
(function () {
  const SAMPLE = {
    isSample: true,
    label: "SAMPLE DATA — DEMONSTRATION ONLY",

    contractor: {
      name: "Sample Plumbing Contractor",
      user: "Sample project manager",
      city: "Phoenix, AZ",
    },

    project: {
      id: "SAMPLE-P-01",
      name: "Sample project — medical office tenant improvement",
      location: "Phoenix, AZ (sample site)",
      stage: "Rough-in",
      bid_due: "2026-10-09",
    },

    // Raw lines as a contractor might upload them (estimating-tool export).
    bom_raw: [
      { line: 1, description: "PEX-A tubing 3/4in x 300ft coil, red", qty: 4, unit: "coil" },
      { line: 2, description: "PEX-A tubing 1/2in x 500ft coil, blue", qty: 3, unit: "coil" },
      { line: 3, description: "Copper pipe type L 1in x 10ft hard", qty: 24, unit: "EA" },
      { line: 4, description: "Ball valve, lead-free brass, full port 3/4in sweat", qty: 18, unit: "each" },
      { line: 5, description: "Ball valve, lead-free brass, full port 1in sweat", qty: 8, unit: "each" },
      { line: 6, description: "PVC DWV pipe sch 40 4in x 10ft", qty: 30, unit: "ea" },
      { line: 7, description: "PVC DWV 4in long-sweep 90 elbow", qty: 22, unit: "ea" },
      { line: 8, description: "Commercial electric water heater 50 gal 208V 3PH", qty: 2, unit: "ea" },
      { line: 9, description: "Reduced pressure backflow preventer 1in", qty: 1, unit: "ea" },
      { line: 10, description: "Wall-hung lavatory w/ carrier", qty: 6, unit: "ea" },
      { line: 11, description: "Floor-mount ADA water closet, 1.28 gpf", qty: 6, unit: "ea" },
      { line: 12, description: "Clevis hanger 1in, galvanized", qty: 60, unit: "ea" },
      { line: 13, description: "Expansion tank 4.4 gal potable", qty: 2, unit: "ea" },
      { line: 14, description: "Misc fittings per plan - see sheet P-201", qty: 1, unit: "lot" },
    ],

    // Neutral placeholder suppliers. Participation is opt-in per supplier.
    suppliers: [
      { id: "A", name: "Sample Supplier A", area: "Central Phoenix", participates: true, response: "Typically responds same day" },
      { id: "B", name: "Sample Supplier B", area: "East Valley", participates: true, response: "Typically responds in 1 day" },
      { id: "C", name: "Sample Supplier C", area: "West Valley", participates: true, response: "Typically responds in 1–2 days" },
    ],

    rfq: {
      id: "RFQ-SAMPLE-1042",
      title: "Rough-in and fixtures — sample medical office TI",
      created: "2026-10-01",
      due: "2026-10-06",
      need_by: "2026-10-14",
      delivery: "Delivered to site",
      status: "Quotes received",
    },

    // Per-supplier status for the sample RFQ.
    rfq_recipients: [
      { supplier: "A", events: [["Sent", "Oct 1, 9:12 AM"], ["Viewed", "Oct 1, 9:40 AM"], ["Quoted", "Oct 1, 2:05 PM"]] },
      { supplier: "B", events: [["Sent", "Oct 1, 9:12 AM"], ["Viewed", "Oct 1, 11:26 AM"], ["Quoted", "Oct 2, 8:30 AM"]] },
      { supplier: "C", events: [["Sent", "Oct 1, 9:12 AM"], ["Viewed", "Oct 2, 7:55 AM"], ["Quoted", "Oct 2, 10:48 AM"]] },
    ],

    // Older sample RFQs for the history list.
    rfq_history: [
      { id: "RFQ-SAMPLE-1031", title: "Underground and DWV — sample retail shell", sent: "2026-09-18", status: "Awarded", note: "Sample Supplier B selected" },
      { id: "RFQ-SAMPLE-1027", title: "Water heater replacement — sample warehouse", sent: "2026-09-09", status: "Awarded", note: "Sample Supplier A selected" },
      { id: "RFQ-SAMPLE-1019", title: "Backflow assemblies — sample school", sent: "2026-08-27", status: "Closed", note: "No award" },
    ],

    // Illustrative sample quotes (unit prices in USD). Not real pricing.
    // avail: "stock" | "days" (lead_days) | "backorder" | "none" (not quoted)
    quotes: {
      A: {
        valid_until: "2026-10-31", delivery_fee: 85, lead_days: 1, terms: "Net 30",
        note: "Water heaters ship from regional warehouse.",
        lines: {
          1: [212.0, "stock"], 2: [188.5, "stock"], 3: [61.4, "stock"], 4: [24.9, "stock"],
          5: [39.75, "stock"], 6: [41.2, "stock"], 7: [17.85, "stock"], 8: [2195.0, "days", 3],
          9: [389.0, "stock"], 10: [342.0, "days", 5], 11: [318.0, "stock"], 12: [2.35, "stock"],
          13: [64.5, "stock"], 14: [1480.0, "stock"],
        },
      },
      B: {
        valid_until: "2026-10-24", delivery_fee: 0, lead_days: 2, terms: "Net 30",
        note: "Free delivery on orders over $5,000. Lavatory carriers backordered.",
        lines: {
          1: [205.0, "stock"], 2: [181.0, "stock"], 3: [63.1, "stock"], 4: [23.6, "stock"],
          5: [38.4, "stock"], 6: [39.9, "stock"], 7: [18.4, "stock"], 8: [2149.0, "days", 4],
          9: [402.0, "stock"], 10: [329.0, "backorder", 21], 11: [309.0, "stock"], 12: [2.2, "stock"],
          13: [61.0, "stock"], 14: [1395.0, "stock"],
        },
      },
      C: {
        valid_until: "2026-10-20", delivery_fee: 125, lead_days: 1, terms: "Net 15",
        note: "Did not quote water heaters. Substitute expansion tank offered (same capacity).",
        lines: {
          1: [219.0, "stock"], 2: [192.0, "stock"], 3: [59.8, "stock"], 4: [25.5, "stock"],
          5: [41.0, "stock"], 6: [40.5, "stock"], 7: [17.2, "stock"], 8: [null, "none"],
          9: [379.0, "stock"], 10: [355.0, "stock"], 11: [321.0, "stock"], 12: [2.45, "stock"],
          13: [58.0, "stock", 0, "Substitute: equivalent 4.5 gal tank"], 14: [1520.0, "stock"],
        },
      },
    },

    // Supplier-side inbox (viewed as Sample Supplier A).
    inbox: [
      { id: "RFQ-SAMPLE-1042", from: "Sample Plumbing Contractor", title: "Rough-in and fixtures — sample medical office TI",
        location: "Phoenix, AZ", received: "Oct 1, 9:12 AM", due: "2026-10-06", lines: 14, status: "New" },
      { id: "RFQ-SAMPLE-1045", from: "Sample Mechanical Contractor", title: "Hydronic piping — sample office remodel",
        location: "Tempe, AZ", received: "Oct 1, 3:47 PM", due: "2026-10-08", lines: 9, status: "Viewed" },
      { id: "RFQ-SAMPLE-1038", from: "Sample Builders", title: "Fixture package — sample restaurant",
        location: "Mesa, AZ", received: "Sep 29, 10:05 AM", due: "2026-10-03", lines: 11, status: "Quoted" },
    ],
  };

  window.CIQ_SAMPLE = Object.freeze(SAMPLE);
})();
