from app.taxonomy import classify_fields


def path(**kwargs):
    return classify_fields(**kwargs).primary_path


def test_claude_code_gets_specific_leaf():
    assert path(title="Anthropic Claude Code agent workflow", caption="Boris explains Claude Code") == (
        "Technology & AI", "Artificial Intelligence", "AI Coding & Agents", "Claude Code"
    )


def test_humanizer_is_ai_writing_not_food():
    assert path(
        title='Comment "slop" and I will send the links',
        caption="#ai #claude #aitools",
        segments={"ocr": ["HUMANIZER checks 26 AI writing patterns and rewrites text"]},
    ) == ("Technology & AI", "Artificial Intelligence", "AI Productivity & Learning", "AI Writing & Humanization")


def test_ai_learning_drills_are_not_fitness():
    assert path(
        title="10 exercise drills that will keep you sharp if you use AI every day",
        caption="Use ChatGPT or Claude without letting it do all the thinking",
        segments={"ocr": ["THE LEARNING AUDIT; keep your ability to start from zero"]},
    ) == ("Technology & AI", "Artificial Intelligence", "AI Productivity & Learning", "AI Learning Skills")


def test_singapore_gp_is_motorsport_not_food_or_travel():
    assert path(
        title="The twisty streets of Singapore await",
        caption="Singapore GP 2026",
        segments={"ocr": ["STREET CIRCUIT MARINA BAY RACE WEEK SINGAPORE GP"]},
    ) == ("Sports & Motorsports", "Motorsport", "Formula 1 & Grand Prix")


def test_police_camera_tracking_is_privacy():
    assert path(
        title="Personal map camera that tracks police cars",
        segments={"ocr": ["automated licence plate recognition technology at 50 intersections"]},
    ) == ("News & Society", "Technology & Society", "Privacy & Surveillance")


def test_bar_content_is_nightlife():
    assert path(
        title="Welcome to Bengaluru",
        segments={"ocr": ["ASIA'S 50 BEST BARS 2023"]},
    ) == ("Food & Drink", "Nightlife", "Bars & Cocktails")


def test_business_models_are_strategy():
    assert path(
        title="Discussing Business Models for IP Creation",
        tags=["business", "ip", "brands", "entrepreneurship"],
    ) == ("Business & Work", "Entrepreneurship", "Business Models & Strategy")


def test_property_sales_is_real_estate():
    assert path(title="Property Sales News", tags=["property", "real estate", "investment"]) == (
        "Finance & Property", "Real Estate", "Property Market"
    )


def test_ocr_performance_is_computer_vision():
    assert path(title="Overview of OCR Model Performance", tags=["ocr", "models", "memory"]) == (
        "Technology & AI", "Artificial Intelligence", "AI Models & Infrastructure", "Computer Vision & OCR"
    )


def test_empty_content_is_uncategorized():
    decision = classify_fields()
    assert decision.primary_path == ("Other", "Uncategorized")
    assert decision.confidence < 0.5


def test_football_name_asr_variant_is_specific():
    assert path(
        title="Has anyone told you look like Steve Copper?",
        segments={"speech": ["Steve Copper is a United Legend former Reading Manager"]},
    ) == ("Sports & Motorsports", "Football", "Football")
