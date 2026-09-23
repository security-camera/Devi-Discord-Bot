"""Bridge between the translation system (i18n.py) and Discord's built-in localization."""

import disnake

import i18n
from i18n import LocaleObject


def get_discord_locale_map() -> dict[str, disnake.Locale]:
    """Creates a mapping: Discord locale code -> disnake.Locale.
    Includes both real locale files and configured aliases.
    """
    supported = set(i18n.available_locale_codes())

    return {
        locale.value: locale
        for locale in disnake.Locale
        if locale.value in supported
    }


def discord_localizations(key: str) -> dict[disnake.Locale, str]:
    """Builds a Discord locale -> translated string mapping.
    Aliases use the translation file of their target locale.
    """
    locale_map = get_discord_locale_map()

    return {
        locale_map[code]: i18n.t(key, locale=code)
        for code in locale_map
    }


def localized(key: str, prefix: str | None = "", postfix: str | None = "") -> disnake.Localized:
    """Creates a disnake.Localized object with translations for all supported Discord locales."""
    return disnake.Localized(
        prefix + i18n.t(key, locale=i18n.DEFAULT_LOCALE) + postfix,
        data=discord_localizations(key),
    )


def bool_to_yes_no_str(b, locale: LocaleObject = None, inversed: bool = False) -> str:
    if not isinstance(b, bool):
        b = bool(b)

    if not inversed:
        return i18n.t("commands.common." + ("yes" if b else "no"), locale=locale)
    else:
        return i18n.t("commands.common." + ("no" if b else "yes"), locale=locale)


def add_remove_choices() -> list[disnake.OptionChoice]:
    return [
        disnake.OptionChoice(name=localized("commands.common.action_add"), value="add"),
        disnake.OptionChoice(name=localized("commands.common.action_remove"), value="remove"),
    ]


def add_remove_check_choices() -> list[disnake.OptionChoice]:
    return add_remove_choices() + [
        disnake.OptionChoice(name=localized("commands.common.action_check"), value="check"),
    ]


def locale_choices(with_flags: bool = True) -> list[disnake.OptionChoice]:
    return [
        disnake.OptionChoice(name=i18n.get_locale_display_name(locale, with_flags), value=locale)
        for locale in i18n.available_locales()
    ]


def yes_no_choices() -> list[disnake.OptionChoice]:
    return [
        disnake.OptionChoice(name=localized("commands.common.yes"), value=True),
        disnake.OptionChoice(name=localized("commands.common.no"), value=False),
    ]