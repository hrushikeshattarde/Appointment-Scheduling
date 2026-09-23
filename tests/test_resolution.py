from facility_profiles.domain.resolution import (
    FacilityResolver,
    KnownFacility,
    ResolutionMethod,
    StopIdentity,
)

KNOWN = KnownFacility(
    facility_id=196546,
    company_name="Carolina Beverage - Super Sport",
    address="119 East Super Sport Drive",
    city="Mooresville",
    state="NC",
    postal_code="28115",
    latitude=35.6234928,
    longitude=-80.8055536,
)


def _stop(**overrides):
    base = dict(
        location_id=None,
        company_name="Carolina Beverage Group",
        address="119 E Super Sport Dr",
        city="Mooresville",
        state="NC",
        postal_code="28115",
        latitude=35.6235,
        longitude=-80.8056,
    )
    base.update(overrides)
    return StopIdentity(**base)


def test_location_id_wins():
    resolver = FacilityResolver([KNOWN])
    res = resolver.resolve(_stop(location_id=123))
    assert res.facility_id == 123
    assert res.method is ResolutionMethod.LOCATION_ID


def test_address_match_in_same_postal_code():
    resolver = FacilityResolver([KNOWN])
    res = resolver.resolve(_stop(latitude=None, longitude=None))
    assert res.facility_id == 196546
    assert res.method is ResolutionMethod.ADDRESS
    assert res.score == 100


def test_geo_plus_name_match_when_address_differs():
    resolver = FacilityResolver([KNOWN])
    res = resolver.resolve(_stop(address="Super Sport Drive Dock 4", postal_code="28115"))
    assert res.facility_id == 196546
    assert res.method is ResolutionMethod.GEO_NAME


def test_unrelated_stop_becomes_candidate_with_stable_key():
    resolver = FacilityResolver([KNOWN])
    a = resolver.resolve(
        _stop(
            company_name="Sweeteners Supply",
            address="1 Mill Rd",
            postal_code="46590",
            city="Wolcott",
            state="IN",
            latitude=None,
            longitude=None,
        )
    )
    b = resolver.resolve(
        _stop(
            company_name="SWEETENERS SUPPLY, INC.",
            address="1 Mill Road",
            postal_code="46590-1234",
            city="Wolcott",
            state="IN",
            latitude=None,
            longitude=None,
        )
    )
    assert a.facility_id is None and b.facility_id is None
    assert a.method is ResolutionMethod.CANDIDATE
    assert a.candidate_key == b.candidate_key
    assert a.key.startswith("candidate:")


def test_far_away_same_name_does_not_match():
    resolver = FacilityResolver([KNOWN])
    res = resolver.resolve(
        _stop(
            address="9 Other St",
            postal_code="90210",
            city="Beverly Hills",
            state="CA",
            latitude=34.09,
            longitude=-118.4,
        )
    )
    assert res.facility_id is None


def test_alias_improves_name_score():
    known = KnownFacility(**{**KNOWN.__dict__, "aliases": {"CBG Mooresville"}})
    resolver = FacilityResolver([known])
    res = resolver.resolve(_stop(company_name="CBG Mooresville", address="Gate 2"))
    assert res.facility_id == 196546
    assert len(resolver) == 1
