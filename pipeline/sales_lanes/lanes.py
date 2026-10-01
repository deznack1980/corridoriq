"""Internal sales-lane keys. Multi-membership is allowed."""

PLUMBING_CORE = "PLUMBING_CORE"
FUEL_GAS_PROPANE = "FUEL_GAS_PROPANE"
FIRE_BACKFLOW = "FIRE_BACKFLOW"
CIVIL_WET_UTILITY = "CIVIL_WET_UTILITY"
HVAC_MECHANICAL = "HVAC_MECHANICAL"
GENERAL_CONTRACTOR_CM = "GENERAL_CONTRACTOR_CM"
MULTI_TRADE = "MULTI_TRADE"
SPECIALTY_OTHER = "SPECIALTY_OTHER"
IDENTITY_REVIEW = "IDENTITY_REVIEW"

SALESPERSON_LANES = (
    PLUMBING_CORE,
    FUEL_GAS_PROPANE,
    FIRE_BACKFLOW,
    CIVIL_WET_UTILITY,
    HVAC_MECHANICAL,
    GENERAL_CONTRACTOR_CM,
    MULTI_TRADE,
    SPECIALTY_OTHER,
)

ALL_LANES = SALESPERSON_LANES + (IDENTITY_REVIEW,)

LANE_META = {
    PLUMBING_CORE: (
        "Plumbing core",
        "Plumbing contractors and mixed plumbing/gas accounts a supply house should call first.",
        1,
    ),
    FUEL_GAS_PROPANE: (
        "Fuel gas / propane",
        "Gas piping and propane contractors, including tank install vs retail exchange.",
        1,
    ),
    FIRE_BACKFLOW: (
        "Fire / backflow",
        "Fire-protection and backflow specialists. Not the plumbing-counter book.",
        1,
    ),
    CIVIL_WET_UTILITY: (
        "Civil wet utility",
        "Water main, sewer, hydrant, and underground wet-utility contractors.",
        1,
    ),
    HVAC_MECHANICAL: (
        "HVAC / mechanical",
        "Mechanical wet-side and HVAC contractors with plumbing-adjacent demand.",
        1,
    ),
    GENERAL_CONTRACTOR_CM: (
        "General contractor / CM",
        "GCs and CMs that influence plumbing purchasing but are not trade contractors.",
        1,
    ),
    MULTI_TRADE: (
        "Multi-trade",
        "Accounts that belong in more than one product motion.",
        1,
    ),
    SPECIALTY_OTHER: (
        "Specialty / other",
        "Landscape-gas, pool, and other adjacent trades that are not plumbing core.",
        1,
    ),
    IDENTITY_REVIEW: (
        "Identity review",
        "NOT_SALES_READY rows kept for analysts. Hidden from default salesperson views.",
        0,
    ),
}

HIGH = "HIGH"
MEDIUM = "MEDIUM"
LOW = "LOW"
EXCLUDED = "EXCLUDED"

# Fuel-gas subtypes. AmeriGas cages ≠ RP Gas Piping.
GAS_PIPING_CONTRACTOR = "GAS_PIPING_CONTRACTOR"
PROPANE_SERVICE = "PROPANE_SERVICE"
PROPANE_TANK_INSTALL = "PROPANE_TANK_INSTALL"
PROPANE_RETAIL_EXCHANGE = "PROPANE_RETAIL_EXCHANGE"
MIXED_PLUMBING_GAS = "MIXED_PLUMBING_GAS"

BOOK_FITS = {HIGH, MEDIUM}
