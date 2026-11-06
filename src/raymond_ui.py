"""Raymond James locator controls: exact active form and ordinary UI events.

The mega-menu's legacy 'Find An Advisor' form is NOT the location-search form.
Never submit a form programmatically or bypass the site's challenge handlers.
"""
from urllib.parse import urlsplit

LOCATION_PANEL = '#faa-v2-search-locations[role="tabpanel"][aria-hidden="false"]'
LOCATION_FORM = 'form#search-locations-v2'
LOCATION_INPUT = 'input#zip-v2'
LOCATION_SUBMIT = 'button#search-locations-page-recaptcha[type="submit"][aria-label="submit location search"]'
PUBLISHED_RADII = {5, 10, 15, 20, 50, 100}
CHALLENGE_FRAMES = (
    'iframe[src*="/recaptcha/api2/bframe"], '
    'iframe[src*="/recaptcha/enterprise/bframe"], '
    'iframe[title*="recaptcha challenge" i], '
    'iframe[src*="hcaptcha.com"][title*="challenge" i]'
)


class RaymondUIError(RuntimeError):
    pass


class RaymondOperatorReviewRequired(RaymondUIError):
    pass


def _validate_timeout(timeout_ms):
    if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or not 1 <= timeout_ms <= 60000:
        raise ValueError('UI timeout must be 1..60000 milliseconds')


async def stop_on_visible_challenge(page):
    """Observe only; never solve, dismiss, or interact with a challenge."""
    parsed = urlsplit(page.url)
    if parsed.hostname == 'errors.edgesuite.net':
        raise RaymondOperatorReviewRequired('Raymond James denied access; operator review required')
    if (parsed.scheme != 'https' or parsed.hostname != 'www.raymondjames.com'
            or parsed.port not in {None, 443} or parsed.username or parsed.password):
        raise RaymondUIError('Location search is not on the Raymond James website')
    frames = page.locator(CHALLENGE_FRAMES)
    count = await frames.count()
    if count > 20: raise RaymondUIError('Unexpected challenge markup; review the locator')
    for index in range(count):
        if await frames.nth(index).is_visible():
            raise RaymondOperatorReviewRequired('Visible verification challenge; operator review required')


async def find_location_search_button(page, *, timeout_ms=30000):
    """Return only the exact active location form's unique visible submit button."""
    _validate_timeout(timeout_ms)
    await stop_on_visible_challenge(page)
    panel = page.locator(LOCATION_PANEL)
    if await panel.count() != 1 or not await panel.is_visible():
        raise RaymondUIError('The active Raymond James location-search panel is not unique and visible')
    form = panel.locator(LOCATION_FORM)
    if await form.count() != 1:
        raise RaymondUIError('The active location-search form is not unique')
    field = form.locator(LOCATION_INPUT)
    if await field.count() != 1 or not await field.is_visible():
        raise RaymondUIError('The location input is not visible in the active search form')
    button = form.locator(LOCATION_SUBMIT)
    if await button.count() != 1 or not await button.is_visible() or not await button.is_enabled():
        raise RaymondUIError('The active location-search submit button is not unique, visible, and enabled')
    return button


async def click_location_search(page, *, timeout_ms=30000):
    """Use a real click so the site's own click/verification handlers can run."""
    button = await find_location_search_button(page, timeout_ms=timeout_ms)
    await button.scroll_into_view_if_needed(timeout=timeout_ms)
    await button.click(timeout=timeout_ms)
    await stop_on_visible_challenge(page)
    return button


async def click_location_radius(page, radius, *, timeout_ms=30000):
    """Click the visible label of a published mileage radio; return changed.

    Called only after ordinary initial search results render. No hidden-input
    click, checked-state assignment, synthetic events, or forced interaction.
    """
    _validate_timeout(timeout_ms)
    if not isinstance(radius, int) or isinstance(radius, bool) or radius not in PUBLISHED_RADII:
        raise RaymondUIError('Requested radius is not a published Raymond James option')
    await stop_on_visible_challenge(page)
    radio = page.locator('input#mileage-' + str(radius) + '[type="radio"][name="mileage"][value="' + str(radius) + '"]')
    label = page.locator('label[for="mileage-' + str(radius) + '"]')
    if await radio.count() != 1 or not await radio.is_enabled():
        raise RaymondUIError('The requested mileage radio is not unique and enabled')
    if await label.count() != 1 or not await label.is_visible():
        raise RaymondUIError('The requested mileage label is not unique and visible')
    if await radio.is_checked(): return False
    await label.scroll_into_view_if_needed(timeout=timeout_ms)
    await label.click(timeout=timeout_ms)
    await stop_on_visible_challenge(page)
    if not await radio.is_checked():
        raise RaymondUIError('The normal mileage-label click did not select the requested radius')
    return True
