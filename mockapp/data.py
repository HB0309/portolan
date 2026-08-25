"""Fixture data and tenant configuration.

Entirely synthetic. Member ids are outside any real numbering scheme and the
names are invented; nothing here is or resembles regulated data.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Account:
    kind: str
    number: str
    balance: str
    opened: str


@dataclass(frozen=True)
class Member:
    member_id: str
    name: str
    status: str
    branch: str
    joined: str
    accounts: list[Account] = field(default_factory=list)

    @property
    def savings_balance(self) -> str:
        for account in self.accounts:
            if account.kind == "Savings":
                return account.balance
        return "0.00"


MEMBERS: dict[str, Member] = {
    "10042": Member(
        member_id="10042",
        name="Dana Whitfield",
        status="Active",
        branch="Riverbend",
        joined="03/14/2016",
        accounts=[
            Account("Savings", "0012-4471", "4,102.55", "03/14/2016"),
            Account("Checking", "0012-4472", "1,288.10", "03/14/2016"),
            Account("Certificate", "0012-4490", "10,000.00", "07/01/2021"),
        ],
    ),
    "10043": Member(
        member_id="10043",
        name="Marcus Oyelaran",
        status="Active",
        branch="Northgate",
        joined="11/02/2019",
        accounts=[
            Account("Savings", "0013-8820", "612.40", "11/02/2019"),
            Account("Checking", "0013-8821", "3,455.02", "11/02/2019"),
        ],
    ),
    "10077": Member(
        member_id="10077",
        name="Priya Raghunathan",
        status="Active",
        branch="Riverbend",
        joined="05/22/2013",
        accounts=[
            Account("Savings", "0017-1002", "22,981.73", "05/22/2013"),
        ],
    ),
    # Servicing staff cannot open this one; used for the permission-denied path.
    "10099": Member(
        member_id="10099",
        name="Restricted Account",
        status="Restricted",
        branch="Corporate",
        joined="01/01/2010",
        accounts=[Account("Savings", "0019-0001", "0.00", "01/01/2010")],
    ),
    "10101": Member(
        member_id="10101",
        name="Wilhelmina Castellanos-Ortiz",
        status="Active",
        branch="Northgate",
        joined="09/09/2009",
        accounts=[
            Account("Savings", "0021-3301", "88,204.19", "09/09/2009"),
            Account("Checking", "0021-3302", "6,742.00", "09/09/2009"),
            Account("Certificate", "0021-3303", "25,000.00", "02/14/2012"),
            Account("Holiday Club", "0021-3304", "1,150.75", "10/01/2020"),
            Account("Vacation Club", "0021-3305", "2,400.00", "06/01/2021"),
        ],
    ),
    # No accounts at all -- the edge case a balance lookup or an account
    # listing has to handle without assuming at least one row exists.
    "10115": Member(
        member_id="10115",
        name="Otieno Kamau",
        status="Active",
        branch="Riverbend",
        joined="04/30/2024",
        accounts=[],
    ),
    # Deliberately awkward for extraction: an apostrophe, a hyphen, and a
    # diacritic in one name, none of which this application escapes specially.
    "10120": Member(
        member_id="10120",
        name="María José O'Brien-Núñez",
        status="Active",
        branch="Corporate",
        joined="12/25/2018",
        accounts=[
            Account("Savings", "0023-9910", "512.33", "12/25/2018"),
        ],
    ),
    "10133": Member(
        member_id="10133",
        name="Grzegorz Wysocki",
        status="Active",
        branch="Northgate",
        joined="08/17/2023",
        accounts=[
            Account("Savings", "0024-1187", "150.00", "08/17/2023"),
            Account("Checking", "0024-1188", "45.60", "08/17/2023"),
        ],
    ),
    # A second restricted member, on a different branch, so the
    # permission-denied path isn't coupled to a single fixture's other
    # properties (branch, join date) by coincidence.
    "10148": Member(
        member_id="10148",
        name="Sealed Estate Account",
        status="Restricted",
        branch="Riverbend",
        joined="01/01/2015",
        accounts=[Account("Savings", "0025-0001", "340,880.00", "01/01/2015")],
    ),
    # Not restricted, but not active either -- a status the UI has to render
    # honestly rather than assuming every member is either Active or Restricted.
    "10156": Member(
        member_id="10156",
        name="Beatrix Sanmiguel",
        status="Closed",
        branch="Corporate",
        joined="03/03/2011",
        accounts=[Account("Savings", "0026-4471", "0.00", "07/15/2024")],
    ),
}

RESTRICTED_MEMBER_IDS = {"10099", "10148"}


@dataclass(frozen=True)
class Tenant:
    """One institution's configuration of the same underlying vendor product.

    The differences are the ones that actually vary between tenants running the
    same software: branding, the captions someone chose during configuration,
    and the order of columns in a grid. The flow underneath is identical, which
    is exactly why one recorded capability ought to serve both.
    """

    tenant_id: str
    institution: str
    product_title: str
    accent: str
    search_button: str
    open_subaccount_link: str
    subaccount_submit: str
    balance_label: str
    accounts_columns_reversed: bool = False


TENANTS: dict[str, Tenant] = {
    "alpha": Tenant(
        tenant_id="alpha",
        institution="Riverbend Credit Union",
        product_title="CoreServ Member Servicing",
        accent="#1b3a5c",
        search_button="Search",
        open_subaccount_link="Open Sub-Account",
        subaccount_submit="Open Sub-Account",
        balance_label="Current Savings Balance",
    ),
    "beta": Tenant(
        tenant_id="beta",
        institution="Cascade Mutual FCU",
        product_title="CoreServ Servicing Console",
        accent="#5c1b2a",
        search_button="Find Member",
        open_subaccount_link="Create Sub Account",
        subaccount_submit="Create Sub Account",
        balance_label="Savings Balance",
        accounts_columns_reversed=True,
    ),
}

DEFAULT_TENANT = "alpha"


#: Runtime conditions this app can be told to produce. Each maps to a tier of
#: the error taxonomy the automation is expected to handle.
INJECTIONS = {
    "none": "Normal operation.",
    # Tier 1 -- legitimate business outcomes, not malfunctions.
    "member_not_found": "Every lookup reports no matching member.",
    "validation_error": "The sub-account form rejects its input.",
    "permission_denied": "Member detail reports insufficient rights.",
    # Tier 2 -- recoverable conditions.
    "interstitial": "A system notice blocks the flow until dismissed.",
    "slow_load": "Pages respond slowly enough to exercise waits.",
    "session_expiry": "The session expires and bounces to login mid-flow.",
    # Tier 3 -- hard failure.
    "server_error": "The application returns a 500.",
}

SUBACCOUNT_TYPES = ["Savings", "Checking", "Holiday Club", "Vacation Club"]
