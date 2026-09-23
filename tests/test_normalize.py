from facility_profiles.domain import normalize as n


def test_html_to_text_keeps_line_breaks_and_unescapes():
    text = n.html_to_text("Line one<br/>Line   two &amp; three<br><br><b>bold</b>")
    assert text == "Line one\nLine two & three\n\nbold"


def test_quote_in_source_is_whitespace_and_case_tolerant():
    source = "Shipper is FCFS   0730-1000\nTrailer must be pre-cooled"
    assert n.quote_in_source("shipper is fcfs 0730-1000", source)
    assert not n.quote_in_source("fcfs 0800", source)
    assert not n.quote_in_source("", source)


def test_phone_normalisation_handles_common_formats():
    assert n.normalize_phone("801.565.6175") == "801-565-6175"
    assert n.normalize_phone("(760) 638-1264") == "760-638-1264"
    assert n.normalize_phone("7606381264") == "760-638-1264"
    assert n.normalize_phone("+1 312-300-7447 ext 8220") == "312-300-7447 x8220"
    assert n.normalize_phone("12345") is None
    assert n.extract_phones("call 615-823-1937 or 615.823.1937 or 555-0100") == ["615-823-1937"]


def test_email_and_url_helpers():
    assert n.normalize_email("USE EMAIL BELOW") is None
    assert n.normalize_email("Giny.Loucks@Example.com, Zach") == "giny.loucks@example.com"
    assert n.extract_emails("a@x.com b@y.org a@x.com") == ["a@x.com", "b@y.org"]
    assert n.normalize_url("https://Booking.DataDocks.com/locations/398/appointments/new/") == (
        "https://booking.datadocks.com/locations/398/appointments/new"
    )
    assert n.normalize_url("www.example.com/sched.") == "https://www.example.com/sched"
    assert n.extract_urls("see https://a.example/x and http://B.example/") == [
        "https://a.example/x",
        "http://b.example",
    ]


def test_company_and_address_normalisation():
    assert n.normalize_company_name("Carolina Beverage Group, LLC") == "carolina beverage group"
    assert n.normalize_company_name("City Brewing Latrobe-Tarrs") == "city brewing latrobe tarrs"
    assert n.normalize_company_name("Acme Co., Inc.") == "acme"
    assert n.normalize_address("1001 Technology Drive West Gate") == "1001 technology dr w gate"
    assert n.normalize_address("955 LOVERS LANE") == "955 lovers ln"
    assert n.normalize_postal("42103-7129") == "42103"
    assert n.normalize_postal("K1A 0B1") == "K1A0B1"


def test_haversine_and_keyword_gate():
    assert n.haversine_m(0, 0, 0, 0) == 0
    assert 110_000 < n.haversine_m(0, 0, 1, 0) < 112_000
    assert n.looks_scheduling_related("Appt requested for 08/03")
    assert n.looks_scheduling_related("Cita para cargar")
    assert not n.looks_scheduling_related("MACROPOINT UPDATE: LOADED")
    assert not n.looks_scheduling_related(None)
