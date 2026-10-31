// Shared CodeMirror setup for the code editor and the version diff: VS Code-like editing, languages loaded on demand.
import { autocompletion, closeBrackets, closeBracketsKeymap, completionKeymap } from '@codemirror/autocomplete'
import { defaultKeymap, history, historyKeymap, indentWithTab } from '@codemirror/commands'
import {
  StreamLanguage, bracketMatching, foldGutter, foldKeymap, indentOnInput, indentUnit, syntaxHighlighting, type LanguageSupport,
} from '@codemirror/language'
import { highlightSelectionMatches, search, searchKeymap } from '@codemirror/search'
import { EditorState, type Extension } from '@codemirror/state'
import {
  EditorView, crosshairCursor, drawSelection, dropCursor, highlightActiveLine, highlightActiveLineGutter, highlightSpecialChars,
  keymap, lineNumbers, rectangularSelection,
} from '@codemirror/view'
import { classHighlighter } from '@lezer/highlight'
import './codemirror.css'

type Loader = () => Promise<LanguageSupport | Extension>
const legacy = (load: () => Promise<Parameters<typeof StreamLanguage.define>[0]>): Loader => async () => StreamLanguage.define(await load())

/** Language keys used by node properties (Code node languages, sql, html, json…). */
const LANGS: Record<string, Loader> = {
  python: async () => (await import('@codemirror/lang-python')).python(),
  javascript: async () => (await import('@codemirror/lang-javascript')).javascript(),
  typescript: async () => (await import('@codemirror/lang-javascript')).javascript({ typescript: true }),
  java: async () => (await import('@codemirror/lang-java')).java(),
  php: async () => (await import('@codemirror/lang-php')).php({ plain: true }),
  go: async () => (await import('@codemirror/lang-go')).go(),
  sql: async () => (await import('@codemirror/lang-sql')).sql(),
  html: async () => (await import('@codemirror/lang-html')).html(),
  json: async () => (await import('@codemirror/lang-json')).json(),
  bash: legacy(async () => (await import('@codemirror/legacy-modes/mode/shell')).shell),
  powershell: legacy(async () => (await import('@codemirror/legacy-modes/mode/powershell')).powerShell),
  perl: legacy(async () => (await import('@codemirror/legacy-modes/mode/perl')).perl),
  ruby: legacy(async () => (await import('@codemirror/legacy-modes/mode/ruby')).ruby),
}

export async function loadLanguage(lang?: string): Promise<Extension> {
  const load = lang ? LANGS[lang.toLowerCase()] : undefined
  if (!load) return []
  try { return await load() } catch { return [] }
}

/** Highlighting via tok-* classes (colours in codemirror.css follow the app theme, VS Code Dark+ / Light+ palettes). */
export const highlight = syntaxHighlighting(classHighlighter)

/** What VS Code gives you out of the box: line numbers, folding, bracket matching / closing, multi-cursor,
 *  Ctrl+F search, Ctrl+/ comments, Tab indent, completion, current-line and selection-match highlights. */
export function editorBasics(): Extension {
  return [
    lineNumbers(), highlightActiveLineGutter(), foldGutter(), highlightSpecialChars(), history(), drawSelection(), dropCursor(),
    EditorState.allowMultipleSelections.of(true), indentOnInput(), bracketMatching(), closeBrackets(), autocompletion(),
    rectangularSelection(), crosshairCursor(), highlightActiveLine(), highlightSelectionMatches(), search({ top: true }),
    indentUnit.of('    '), highlight,
    keymap.of([...closeBracketsKeymap, ...defaultKeymap, ...searchKeymap, ...historyKeymap, ...foldKeymap, ...completionKeymap, indentWithTab]),
  ]
}

/** Read-only viewer (diffs): line numbers and highlighting, no editing chrome. */
export function viewerBasics(): Extension {
  return [lineNumbers(), highlightSpecialChars(), drawSelection(), highlightSelectionMatches(), highlight,
    EditorView.editable.of(false), keymap.of([...defaultKeymap, ...searchKeymap]), search({ top: true })]
}
