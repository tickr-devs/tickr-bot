"""The apps' Swift code rules (apps/scripts/check_swift_rules.py): words, indentation, catalog."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "apps" / "scripts"


def load():
    sys.path.insert(0, str(SCRIPTS))
    script = SCRIPTS / "check_swift_rules.py"
    spec = importlib.util.spec_from_file_location("check_swift_rules", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def write(tmp_path: Path, source: str, name: str = "View.swift") -> Path:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return path


def test_flags_words_in_ui_code(tmp_path):
    source = (
        'Text("Overview")\n'
        'Button("Quit") {}\n'
        'Text(verbatim: "Aperture")\n'
        'var title: String { return "Flatten All" }\n'
        'let s = String(localized: "Pause entries?")\n'
    )
    found = load().text_violations(write(tmp_path, source))
    assert [line for line, _ in found] == [1, 2, 3, 4, 5]


def test_allows_symbols_constants_raw_values_and_punctuation(tmp_path):
    source = (
        'Image(systemName: "gearshape")\n'
        'Label(L10n.general, systemImage: "bell.badge")\n'
        '.init(symbol: "chart.xyaxis.line")\n'
        "/// User-defaults key of the API address.\n"
        'static let apiBaseURL = "apiBaseURL"\n'
        'enum Kind: String { case foreignExposure = "foreign_exposure" }\n'
        'let line = "\\(day) · \\(name)"\n'
        'let minus = "\\u{2212}"\n'
        '@available(*, deprecated, renamed: "surface") var card: Color\n'
        '// Text("in a comment")\n'
        "let empty = \"\"\n"
    )
    assert load().text_violations(write(tmp_path, source)) == []


def test_reads_literals_inside_interpolations(tmp_path):
    source = 'let a = "\\(ok ? "\\(n)" : "none")"\n'
    assert load().text_violations(write(tmp_path, source)) == [(1, "none")]


def test_l10n_file_is_the_one_place_for_words(tmp_path):
    source = (
        'static let quit = LocalizedStringResource("app.quit", defaultValue: "Quit")\n'
        'static func open(_ name: String) -> LocalizedStringResource {\n'
        '  LocalizedStringResource("app.open",\n'
        '    defaultValue: "Open \\(name)")\n'
        "}\n"
    )
    assert load().text_violations(write(tmp_path, source, "L10n.swift")) == []


def test_l10n_file_holds_only_localized_entries(tmp_path):
    source = (
        'static let quit = "Quit"\nlet title = Text("Quit now")\n'
        'static let all = make(defaultValue: "Flatten every position")\n'
    )
    found = load().text_violations(write(tmp_path, source, "L10n.swift"))
    assert found == [(1, "Quit"), (2, "Quit now"), (3, "Flatten every position")]


def test_words_cannot_hide_in_constants_raw_values_or_symbol_shapes(tmp_path):
    source = (
        'enum Section: String { case overview = "Overview of the day" }\n'
        'enum Tab: String { case home = "Home" }\n'
        "/// The window title.\n"
        'static let title = "Strategy lab"\n'
        'Text("settings.window")\n'
        'case .lab: return "sparkles"\n'
    )
    found = load().text_violations(write(tmp_path, source))
    assert [line for line, _ in found] == [1, 2, 4, 5, 6]


def test_indentation_is_two_spaces(tmp_path):
    source = "struct A {\n  var b: Int {\n      1\n  }\n\tlet c = 2\n   let d = 3\n}\n"
    found = load().indent_violations(write(tmp_path, source))
    assert found == [(3, "1"), (5, "tab"), (6, "let d = 3")]


def test_every_l10n_key_needs_a_russian_translation(tmp_path):
    l10n = write(
        tmp_path,
        'static let quit = LocalizedStringResource("app.quit", defaultValue: "Quit")\n'
        'static let open = LocalizedStringResource("app.open", defaultValue: "Open")\n',
        "L10n.swift",
    )

    def unit(language: str, value: str) -> dict:
        string_unit = {"stringUnit": {"state": "translated", "value": value}}
        return {"localizations": {language: string_unit}}

    catalog = {
        "sourceLanguage": "en",
        "strings": {
            "app.quit": unit("ru", "Выйти"),
            "app.open": unit("en", "Open"),
        },
    }
    (tmp_path / "Localizable.xcstrings").write_text(json.dumps(catalog), encoding="utf-8")
    assert load().catalog_violations(l10n) == [(2, "app.open")]


def test_repository_matches_its_baseline():
    assert load().main([]) == 0


def test_a_pull_request_cannot_add_to_the_baseline():
    module = load()
    base = {"text": {"a.swift": ["Old", "Old"]}}
    assert module.raised({"text": {"a.swift": ["Old", "Old"]}}, base) == 0
    assert module.raised({"text": {"a.swift": ["Old"]}}, base) == 0
    assert module.raised({"text": {"a.swift": ["Old", "Old", "Old"]}}, base) == 1
    assert module.raised({"text": {"a.swift": ["New"]}}, base) == 1
    assert module.raised({"text": {"b.swift": ["Old"]}}, base) == 1
    assert module.raised({"text": {"b.swift": ["Old"]}}, None) == 0


def test_removing_an_old_violation_does_not_make_room_for_a_new_one(tmp_path, monkeypatch):
    module = load()
    path = write(tmp_path, 'Text("Brand new")\n')
    monkeypatch.setattr(module, "REPO", tmp_path)
    monkeypatch.setattr(module, "ROOTS", (".",))
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"text": {path.name: ["Old text"]}}), encoding="utf-8")
    monkeypatch.setattr(module, "BASELINE", baseline)
    assert module.main([]) == 1


def test_info_plist_texts_need_a_russian_translation(tmp_path):
    import plistlib

    support = tmp_path / "App" / "Support"
    support.mkdir(parents=True)
    plist = support / "Info.plist"
    plist.write_bytes(plistlib.dumps({"NSFaceIDUsageDescription": "Confirms commands."}))
    assert load().plist_violations(plist) == [(1, "NSFaceIDUsageDescription")]
    catalog = {
        "strings": {
            "NSFaceIDUsageDescription": {
                "localizations": {"ru": {"stringUnit": {"state": "translated", "value": "Да"}}}
            }
        }
    }
    (tmp_path / "App" / "InfoPlist.xcstrings").write_text(json.dumps(catalog), encoding="utf-8")
    assert load().plist_violations(plist) == []
