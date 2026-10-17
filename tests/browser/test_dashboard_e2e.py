"""Browser end-to-end test suite for the NeuroRoute dashboard.
Tests all 10 core browser workflows against an isolated test server:
1. Dashboard loads successfully.
2. Main navigation and major dashboard sections work.
3. Model-management interface opens and closes.
4. Model form validation displays appropriate feedback.
5. Model-management actions work against an isolated test backend.
6. Routing requests display their results.
7. Decision traces and rejection reasons render correctly.
8. Request history displays available results.
9. BYOK forms do not reveal previously stored API keys.
10. Invalid inputs and failed requests produce understandable errors.
"""
import pytest

try:
    from playwright.sync_api import Page, expect
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright is required for browser e2e tests"),
]


def test_dashboard_initial_load(page: "Page"):
    """Workflow 1: Dashboard loads successfully with title, branding, and online status."""
    expect(page).to_have_title("NeuroRoute 2026 · Dynamic AI Model Router")

    # Branding header
    expect(page.locator("header")).to_be_visible()
    expect(page.locator("header").get_by_text("NEUROROUTE")).to_be_visible()

    # Connection indicator
    conn_label = page.locator("#connLabel")
    expect(conn_label).to_have_text("gateway online")

    # Headline
    headline = page.locator("h1")
    expect(headline).to_contain_text("AI models. One routes for you.")


def test_main_navigation_sections(page: "Page"):
    """Workflow 2: Main navigation links and all major dashboard sections are present and reachable."""
    sections = [
        ("#model-manager", "Model Manager"),
        ("#models", "Model Fleet"),
        ("#request-console", "Send a Request"),
        ("#matrix-visualizer", "Live Routing Tally"),
        ("#request-history", "Request History"),
    ]

    for selector, heading_text in sections:
        section = page.locator(selector)
        expect(section).to_be_visible()
        expect(section.get_by_text(heading_text, exact=False).first).to_be_visible()

    # Test navigation bar links
    nav = page.locator("nav")
    expect(nav.get_by_role("link", name="Model Manager")).to_have_attribute("href", "#model-manager")
    expect(nav.get_by_role("link", name="Model Fleet")).to_have_attribute("href", "#models")
    expect(nav.get_by_role("link", name="Playground")).to_have_attribute("href", "#request-console")
    expect(nav.get_by_role("link", name="Live Mesh")).to_have_attribute("href", "#matrix-visualizer")
    expect(nav.get_by_role("link", name="History")).to_have_attribute("href", "#request-history")


def test_model_manager_modal_open_close(page: "Page"):
    """Workflow 3: Model-management modal opens and closes reliably via close button and cancel button."""
    modal = page.locator("#modelModal")
    expect(modal).to_be_hidden()

    # Click + Add Model on slot 1 card
    add_btn = page.locator("#slot-card-1 .addBtn")
    add_btn.click()

    # Modal should now be open
    expect(modal).to_be_visible()
    expect(page.locator("#modalTitle")).to_contain_text("Add Model")

    # Close via close (X) button
    page.locator("#closeModalBtn").click()
    expect(modal).to_be_hidden()

    # Re-open and close via Cancel button
    add_btn.click()
    expect(modal).to_be_visible()
    page.locator("#cancelModalBtn").click()
    expect(modal).to_be_hidden()


def test_model_form_validation(page: "Page"):
    """Workflow 4: Model form client/server validation displays feedback for invalid fields."""
    page.locator("#slot-card-1 .addBtn").click()
    modal = page.locator("#modelModal")
    expect(modal).to_be_visible()

    # Clear model name to trigger validation
    model_name_input = page.locator("#formModelName")
    model_name_input.fill("")

    # Attempt to submit
    page.locator("#saveModelBtn").click()

    # Enter an invalid context limit (0)
    model_name_input.fill("test-invalid-model")
    page.locator("#formContextLimit").fill("0")
    page.locator("#saveModelBtn").click()

    # Verify HTML5 input:invalid blocks or error box appears
    is_invalid = page.evaluate("() => document.getElementById('formContextLimit').validity.valid === false")
    error_box = page.locator("#modalErrorBox")
    assert is_invalid or error_box.is_visible()

    # Clean up modal
    page.locator("#closeModalBtn").click()


def test_model_management_actions_and_demo_loading(page: "Page"):
    """Workflow 5: Model-management actions (load demo, toggle enabled, test ping, reset) against isolated backend."""
    # 1. Load demo models
    load_demo_btn = page.locator("#loadDemoBtn")
    load_demo_btn.click()

    # Wait for slot cards to become occupied
    slot1_card = page.locator("#slot-card-1")
    expect(slot1_card.locator("h4")).to_contain_text("nano-1", timeout=5000)
    expect(page.locator("#fleetCountBadge")).to_contain_text("6/6 UNITS ONLINE")

    # 2. Toggle routing availability on slot 1
    toggle_btn = slot1_card.locator(".togSlotBtn")
    toggle_btn.click()

    # Slot 1 should now indicate DISABLED
    expect(slot1_card.get_by_text("DISABLED", exact=True)).to_be_visible(timeout=5000)

    # 3. Run simulated test trial on slot 1
    test_btn = slot1_card.locator(".testSlotBtn")
    test_btn.click()
    expect(slot1_card.locator("#testResult-slot-1")).to_contain_text("TEST PASSED", timeout=5000)

    # 4. Reset workspace fleet
    page.once("dialog", lambda d: d.accept())
    page.locator("#resetFleetBtn").click()
    # All slots should now be empty
    expect(slot1_card.get_by_text("EMPTY", exact=True)).to_be_visible(timeout=5000)


def test_routing_requests_display_results(page: "Page"):
    """Workflow 6: Dispatched routing requests display comprehensive result blocks in Playground."""
    # Load demo models first
    page.locator("#loadDemoBtn").click()
    expect(page.locator("#slot-card-1 h4")).to_contain_text("nano-1", timeout=5000)

    # Fill prompt
    page.locator("#promptInput").fill("What is the capital of France?")

    # Click Route Request button
    page.locator("#btnRoute").click()

    # Result box should appear
    result_box = page.locator("#routingResultBox")
    expect(result_box).to_be_visible(timeout=5000)
    expect(result_box).to_contain_text("ROUTED TO TARGET")
    expect(result_box).to_contain_text("Selected Target:")


def test_decision_trace_and_rejection_reasons(page: "Page"):
    """Workflow 7: Decision traces render considered candidates with explicit rejection reasons."""
    page.locator("#loadDemoBtn").click()
    expect(page.locator("#slot-card-1 h4")).to_contain_text("nano-1", timeout=5000)

    # Set very strict latency constraint (e.g. 400ms) so models with latency > 400ms are rejected
    page.locator("#inputMaxLat").fill("400")
    page.locator("#promptInput").fill("Analyze multi-step financial risk")

    page.locator("#btnRoute").click()

    result_box = page.locator("#routingResultBox")
    expect(result_box).to_be_visible(timeout=5000)

    # Check that candidate evaluation / decision trace section is rendered
    expect(result_box.get_by_text("Candidate Evaluation:")).to_be_visible()
    # Check for rejection badge / reasons
    expect(result_box).to_contain_text("latency")


def test_request_history_displays_results(page: "Page"):
    """Workflow 8: Request history table renders logged requests for the active workspace."""
    # Load models and send request
    page.locator("#loadDemoBtn").click()
    page.locator("#promptInput").fill("Audited history query test")
    page.locator("#btnRoute").click()
    expect(page.locator("#routingResultBox")).to_be_visible(timeout=5000)

    # Refresh history table
    page.locator("#refreshHistoryBtn").click()

    # History container should render table rows
    history_container = page.locator("#historyTableContainer")
    expect(history_container.locator("table")).to_be_visible(timeout=5000)
    expect(history_container).to_contain_text("Audited history query test")


def test_byok_form_write_only_key_handling(page: "Page"):
    """Workflow 9: BYOK forms handle API keys write-only and do not reveal stored secrets."""
    # Open modal on slot 1
    page.locator("#slot-card-1 .addBtn").click()
    modal = page.locator("#modelModal")
    expect(modal).to_be_visible()

    # Switch to Live mode
    page.locator("#formExecutionMode").select_option("live")

    # API key container and warning banner should appear
    api_key_container = page.locator("#apiKeyContainer")
    expect(api_key_container).to_be_visible()
    expect(page.locator("#liveWarningBanner")).to_be_visible()

    # Fill required fields
    page.locator("#formModelName").fill("live-gpt-mini")
    page.locator("#formModelId").fill("gpt-4o-mini")
    page.locator("#formApiKey").fill("sk-test-live-key-9988776655")

    # Submit
    page.locator("#saveModelBtn").click()
    expect(modal).to_be_hidden(timeout=5000)

    # Slot 1 should now be occupied with LIVE badge
    slot1 = page.locator("#slot-card-1")
    expect(slot1.get_by_text("LIVE", exact=True)).to_be_visible(timeout=5000)
    expect(slot1.get_by_text("key ····6655")).to_be_visible()

    # Click Edit on Slot 1
    slot1.locator(".editBtn").click()
    expect(modal).to_be_visible()

    # Assert form API key input is EMPTY (write-only security guarantee)
    api_key_input = page.locator("#formApiKey")
    expect(api_key_input).to_have_value("")

    # Assert key saved notice is shown with masked last4
    key_saved_notice = page.locator("#keySavedNotice")
    expect(key_saved_notice).to_be_visible()
    expect(key_saved_notice).to_contain_text("Key saved ····6655 — leave blank to keep it")

    # Clean up modal
    page.locator("#closeModalBtn").click()


def test_invalid_inputs_and_error_handling(page: "Page"):
    """Workflow 10: Invalid inputs or impossible constraints produce clear refusal (NO_VALID_MODEL)."""
    # Empty workspace has 0 models
    page.once("dialog", lambda d: d.accept())
    page.locator("#resetFleetBtn").click()
    expect(page.locator("#slot-card-1").get_by_text("EMPTY", exact=True)).to_be_visible(timeout=5000)

    # Route request with 0 models in fleet
    page.locator("#promptInput").fill("Query when 0 models configured")
    page.locator("#btnRoute").click()

    # Should clearly refuse with NO_VALID_MODEL
    result_box = page.locator("#routingResultBox")
    expect(result_box).to_be_visible(timeout=5000)
    expect(result_box).to_contain_text("NO VALID MODEL")
    expect(result_box).to_contain_text("REQUEST REFUSED")
