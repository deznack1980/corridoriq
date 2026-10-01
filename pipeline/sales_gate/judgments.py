"""Manual Top-25 judgments. Ranking is not changed. Wrong accounts are flagged."""

SALES_READY = "SALES_READY"
CAUTION = "SALES_READY_WITH_CAUTION"
RESEARCH = "RESEARCH_FIRST"
NOT_READY = "NOT_SALES_READY"

READY_OUTREACH = "READY_FOR_OUTREACH"
CONTACTABLE_UNCLEAR = "CONTACTABLE_BUT_DEMAND_UNCLEAR"
DEMAND_WEAK_CONTACT = "DEMAND_CLEAR_BUT_CONTACT_WEAK"
IDENTITY_UNRESOLVED = "IDENTITY_UNRESOLVED"
NOT_FULFILL = "NOT_READY"

# company_id -> overlay. Live metrics still come from the database.
JUDGMENTS = {
    40676: {  # Arizona Propane
        "segment": "FUEL_GAS_PROPANE",
        "readiness": SALES_READY,
        "action": "Call the Fountain Hills office; ask for the permit/ops desk about current tank and gas-line jobs.",
        "fulfillment": READY_OUTREACH,
        "flags": [],
    },
    38317: {  # Canyon State Propane
        "segment": "FUEL_GAS_PROPANE",
        "readiness": SALES_READY,
        "action": "Call the published business line; ask for purchasing or the installer dispatching the ASME tank jobs.",
        "fulfillment": READY_OUTREACH,
        "flags": [],
    },
    47553: {  # R C I SYSTEMS INC
        "segment": "FIRE_BACKFLOW",
        "readiness": CAUTION,
        "action": "Do not treat as a plumber. If the house sells fire/backflow, call Todd Little / main line about 6-inch backflow replacements.",
        "fulfillment": READY_OUTREACH,
        "flags": ["wrong_trade_slot", "duplicate_of_43066", "fire_protection_not_plumbing"],
    },
    39004: {  # RP GAS PIPING
        "segment": "FUEL_GAS_PROPANE",
        "readiness": SALES_READY,
        "action": "Call 602-759-8340 or email info@; current Coakley/Asher gas-piping jobs are the opener.",
        "fulfillment": READY_OUTREACH,
        "flags": [],
    },
    43066: {  # RCI SYSTEMS, INC.
        "segment": "FIRE_BACKFLOW",
        "readiness": CAUTION,
        "action": "Same commercial account as R C I SYSTEMS INC (canonical initials variant). Do not double-call.",
        "fulfillment": READY_OUTREACH,
        "flags": ["duplicate_of_47553", "wrong_trade_slot", "fire_protection_not_plumbing"],
    },
    37709: {  # Millennium Gas
        "segment": "FUEL_GAS_PROPANE",
        "readiness": SALES_READY,
        "action": "Email bids@ or call 480-633-2176 about underground/aboveground propane tank installs.",
        "fulfillment": READY_OUTREACH,
        "flags": ["primary_demand_taxonomy_says_water_heater_observed_is_propane_tanks"],
    },
    41624: {  # Propane Services
        "segment": "FUEL_GAS_PROPANE",
        "readiness": SALES_READY,
        "action": "Call Douglas Hoffpauir / Hillary inbox; 500-gal underground LPG tanks are the current work.",
        "fulfillment": READY_OUTREACH,
        "flags": [],
    },
    37857: {  # Parker & Sons
        "segment": "MULTI_TRADE",
        "readiness": CAUTION,
        "action": "Call the published Parker and Sons line; confirm you are talking to Environmental Conditioning LLC ops, not the steel-erecting cousin. Opener: recent propane tanks plus huge water-heater/HVAC history.",
        "fulfillment": READY_OUTREACH,
        "flags": ["identity_high_not_verified", "hvac_brand_with_cr37", "steel_erecting_namesake_exists"],
    },
    45367: {  # AmeriGas
        "segment": "FUEL_GAS_PROPANE",
        "readiness": CAUTION,
        "action": "Recent work is LPG exchange cages, not contractor gas piping. Call only if the house sells cage/tank programs; otherwise skip scarce plumber time.",
        "fulfillment": CONTACTABLE_UNCLEAR,
        "flags": ["retail_exchange_cages", "no_roc_match"],
    },
    37617: {  # Kris Espinoza
        "segment": "UNKNOWN",
        "readiness": NOT_READY,
        "action": "Do not call. Person-name Mesa pool/gas-line permits; no ROC license. Leave NOT_SALES_READY.",
        "fulfillment": NOT_FULFILL,
        "flags": ["person_name", "permit_applicant", "no_actionable_channel"],
    },
    38307: {  # Gas Piping Inc
        "segment": "FUEL_GAS_PROPANE",
        "readiness": CAUTION,
        "action": "HIGH ROC R-37R. Use the CANDIDATE BBB/directory phone; do not treat the inferred email as verified.",
        "fulfillment": DEMAND_WEAK_CONTACT,
        "flags": ["contact_candidate_only", "official_site_did_not_confirm"],
    },
    38349: {  # Kerns Plumbing
        "segment": "RESIDENTIAL_PLUMBING",
        "readiness": SALES_READY,
        "action": "Call scheduling@ / 480-264-3990. Verified CR-37 plumber with current named gas-piping jobs.",
        "fulfillment": READY_OUTREACH,
        "flags": [],
    },
    38579: {  # Ferrell Gas
        "segment": "FUEL_GAS_PROPANE",
        "readiness": CAUTION,
        "action": "National propane. Mix of exchange-program cages and tank/gas-line work. Use Phoenix location 602-278-8511, not only the 888 line.",
        "fulfillment": CONTACTABLE_UNCLEAR,
        "flags": ["national_brand", "no_roc_match", "cage_and_tank_mix"],
    },
    40911: {  # Metering Services
        "segment": "FIRE_BACKFLOW",
        "readiness": CAUTION,
        "action": "Backflow/metering house. Call Keri Frampton if the counter sells assemblies; not a fixture plumber.",
        "fulfillment": READY_OUTREACH,
        "flags": ["wrong_trade_slot", "backflow_not_fixture_plumbing"],
    },
    45893: {  # Midwest Contracting
        "segment": "CIVIL_WET_UTILITY",
        "readiness": CAUTION,
        "action": "Verified A-12 civil contractor. Current jobs are fire hydrant/backflow/civil water, not plumbing fixtures. Route to wet-utility sales.",
        "fulfillment": READY_OUTREACH,
        "flags": ["wrong_trade_slot", "civil_not_plumber"],
    },
    37828: {  # Metro Fire
        "segment": "FIRE_BACKFLOW",
        "readiness": CAUTION,
        "action": "Fire alarm/suppression contractor. Do not occupy plumber Top-25 time unless the house has a fire/backflow counter.",
        "fulfillment": READY_OUTREACH,
        "flags": ["wrong_trade_slot", "fire_protection_not_plumbing"],
    },
    41390: {  # ABC Water Works
        "segment": "FIRE_BACKFLOW",
        "readiness": CAUTION,
        "action": "CR-37 plumber whose current observed jobs are HOA/grocery backflow. Callable; opener is backflow assemblies, not fixture packages.",
        "fulfillment": READY_OUTREACH,
        "flags": ["licensed_plumber_current_demand_is_backflow"],
    },
    38527: {  # Creative Environments
        "segment": "SPECIALTY_OTHER",
        "readiness": CAUTION,
        "action": "Landscape/outdoor living installing PE gas to firepits. Callable for gas pipe/fittings; not a plumbing contractor.",
        "fulfillment": READY_OUTREACH,
        "flags": ["landscape_gas", "wrong_trade_slot"],
    },
    47151: {  # United Integrated Services
        "segment": "GENERAL_CONTRACTOR_CM",
        "readiness": CAUTION,
        "action": "Semiconductor GC with B-1 plus C-37. High volume of confidential fab permits. Do not treat as a local plumbing counter account; CM/purchasing track only.",
        "fulfillment": CONTACTABLE_UNCLEAR,
        "flags": ["gc_cm", "confidential_fab_jobs", "has_c37_but_not_trade_contractor_behavior"],
    },
    41108: {  # ABS Arizona Backflow
        "segment": "FIRE_BACKFLOW",
        "readiness": CAUTION,
        "action": "EPCOR-list phone only; no official website. Call 602-548-1101 about backflow replacements if that lane exists.",
        "fulfillment": DEMAND_WEAK_CONTACT,
        "flags": ["no_official_website", "candidate_phone_only"],
    },
    37979: {  # Rider Permit Service
        "segment": "SPECIALTY_OTHER",
        "readiness": NOT_READY,
        "action": "Permit expeditor filing pool/gas permits for others. No ROC license, no official site. Do not store directory numbers as verified. Skip.",
        "fulfillment": NOT_FULFILL,
        "flags": ["permit_expeditor", "no_official_channel", "not_the_buying_contractor"],
    },
    47465: {  # Austin Commercial
        "segment": "GENERAL_CONTRACTOR_CM",
        "readiness": CAUTION,
        "action": "National CM. Contactable via James Augustyn, but this is not a plumbing-supply counter account.",
        "fulfillment": CONTACTABLE_UNCLEAR,
        "flags": ["gc_cm", "confidential_jobs", "wrong_trade_slot"],
    },
    45906: {  # Petra
        "segment": "CIVIL_WET_UTILITY",
        "readiness": CAUTION,
        "action": "Identity is K C R Inc (license 119815 on permit). Call Keith Riefkohl about sewer-main work, not fixture plumbing. FLAG plumbing_specialist label.",
        "fulfillment": READY_OUTREACH,
        "flags": ["wrong_trade_slot", "civil_not_plumber", "production_match_conflict_is_capability_not_identity"],
    },
    37788: {  # Stephanie Sekona
        "segment": "UNKNOWN",
        "readiness": NOT_READY,
        "action": "Do not call. Person-name Mesa pool/gas-line permits; zero ROC hits. Leave NOT_SALES_READY.",
        "fulfillment": NOT_FULFILL,
        "flags": ["person_name", "permit_applicant", "no_actionable_channel"],
    },
    45869: {  # Arrowmark
        "segment": "CIVIL_WET_UTILITY",
        "readiness": CAUTION,
        "action": "Verified class A. Current jobs are fire-hydrant relocation/abandonment. Email bids@ if the house sells civil/hydrant material; not a plumber.",
        "fulfillment": READY_OUTREACH,
        "flags": ["wrong_trade_slot", "civil_not_plumber"],
    },
}
