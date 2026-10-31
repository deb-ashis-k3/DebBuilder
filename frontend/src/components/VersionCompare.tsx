import {
  Background,
  BackgroundVariant,
  Controls,
  ReactFlow,
  ReactFlowProvider,
  type ReactFlowInstance,
  type Viewport,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { Icon, toast, useCatalog } from "../lib";
import { edgeToRF, toRF } from "../pages/Editor";
import type {
  NodeTypeSpec,
  WFEdge,
  WFNode,
  Workflow,
  WorkflowVersion,
} from "../types";
import { edgeTypes } from "./CanvasEdge";
import { FitWhenReady } from "../canvas/FitWhenReady";
import { CodeDiff } from "./CodeDiff";
import { nodeTypes } from "./FlowNode";
import { Select } from "./Select";

/** Compare two versions of a workflow (n8n's workflow diff): the published one, the editor's draft or any
 *  earlier version. Two read-only canvases with added / removed / changed nodes highlighted, the list of
 *  changes, and a line diff of the selected node's configuration. */

export const DRAFT = "draft";
type Graph = { nodes: WFNode[]; edges: WFEdge[] };
type Kind = "added" | "removed" | "changed";
interface NodeChange {
  kind: Kind;
  id: string;
  name: string;
  type: string;
  fields: string[];
}
interface EdgeChange {
  kind: "added" | "removed";
  key: string;
  label: string;
}

/** What counts as a node's configuration: position and pinned test data don't. */
const config = (n: WFNode) => ({
  name: n.name,
  type: n.type,
  disabled: !!n.disabled,
  parameters: n.parameters ?? {},
  settings: n.settings ?? {},
  ...(n.type === "ui.sticky"
    ? { size: [n.width ?? null, n.height ?? null] }
    : {}),
});

/** JSON with sorted keys, so key order never shows up as a change. */
function stable(v: unknown, indent = 2): string {
  const sort = (x: unknown): unknown =>
    Array.isArray(x)
      ? x.map(sort)
      : x && typeof x === "object"
        ? Object.fromEntries(
            Object.keys(x as object)
              .sort()
              .map((k) => [k, sort((x as Record<string, unknown>)[k])]),
          )
        : x;
  return JSON.stringify(sort(v), null, indent) ?? "null";
}

const edgeKey = (e: WFEdge) =>
  `${e.source}|${e.sourceHandle ?? "main"}|${e.target}|${e.targetHandle ?? "main"}`;

export function diffGraphs(a: Graph, b: Graph) {
  const an = new Map(a.nodes.map((n) => [n.id, n]));
  const bn = new Map(b.nodes.map((n) => [n.id, n]));
  const nodes: NodeChange[] = [];
  let moved = 0;
  for (const n of b.nodes) {
    const old = an.get(n.id);
    if (!old) {
      nodes.push({
        kind: "added",
        id: n.id,
        name: n.name,
        type: n.type,
        fields: [],
      });
      continue;
    }
    const ca = config(old),
      cb = config(n);
    const fields: string[] = [];
    if (ca.name !== cb.name) fields.push("name");
    if (ca.disabled !== cb.disabled)
      fields.push(cb.disabled ? "disabled" : "enabled");
    for (const k of new Set([
      ...Object.keys(ca.parameters),
      ...Object.keys(cb.parameters),
    ])) {
      if (
        stable(ca.parameters[k] ?? null, 0) !==
        stable(cb.parameters[k] ?? null, 0)
      )
        fields.push(k);
    }
    if (stable(ca.settings, 0) !== stable(cb.settings, 0))
      fields.push("settings");
    if (
      stable((ca as { size?: unknown }).size ?? null, 0) !==
      stable((cb as { size?: unknown }).size ?? null, 0)
    )
      fields.push("size");
    if (fields.length)
      nodes.push({
        kind: "changed",
        id: n.id,
        name: n.name,
        type: n.type,
        fields,
      });
    else if (
      old.position?.x !== n.position?.x ||
      old.position?.y !== n.position?.y
    )
      moved++;
  }
  for (const n of a.nodes)
    if (!bn.has(n.id))
      nodes.push({
        kind: "removed",
        id: n.id,
        name: n.name,
        type: n.type,
        fields: [],
      });
  const name = (id: string) => bn.get(id)?.name ?? an.get(id)?.name ?? id;
  const ak = new Set(a.edges.map(edgeKey)),
    bk = new Set(b.edges.map(edgeKey));
  const label = (e: WFEdge) =>
    `${name(e.source)}${e.sourceHandle && e.sourceHandle !== "main" ? ` (${e.sourceHandle})` : ""} → ${name(e.target)}${e.targetHandle && e.targetHandle !== "main" ? ` (${e.targetHandle})` : ""}`;
  const edges: EdgeChange[] = [
    ...b.edges
      .filter((e) => !ak.has(edgeKey(e)))
      .map((e) => ({
        kind: "added" as const,
        key: edgeKey(e),
        label: label(e),
      })),
    ...a.edges
      .filter((e) => !bk.has(edgeKey(e)))
      .map((e) => ({
        kind: "removed" as const,
        key: edgeKey(e),
        label: label(e),
      })),
  ];
  const order = { changed: 0, added: 1, removed: 2 };
  nodes.sort(
    (x, y) => order[x.kind] - order[y.kind] || x.name.localeCompare(y.name),
  );
  return {
    nodes,
    edges,
    moved,
    addedEdges: new Set(
      edges.filter((e) => e.kind === "added").map((e) => e.key),
    ),
    removedEdges: new Set(
      edges.filter((e) => e.kind === "removed").map((e) => e.key),
    ),
  };
}

/** One side of a field as text: strings as they are (code shows as real lines), everything else as JSON. */
const asText = (v: unknown) =>
  v === undefined ? "" : typeof v === "string" ? v : stable(v);

interface FieldDiff {
  key: string;
  label: string;
  before: string;
  after: string;
  language?: string;
  changed: boolean;
}

/** The node's configuration field by field: name, enabled, each parameter, settings. */
function fieldDiffs(
  a: WFNode | undefined,
  b: WFNode | undefined,
  spec?: NodeTypeSpec,
): FieldDiff[] {
  const specProps = spec?.properties ?? [];
  const props = new Map(specProps.map((p) => [p.name, p]));
  const pa = a?.parameters ?? {},
    pb = b?.parameters ?? {};
  const lang = String(pb.language ?? pa.language ?? "python");
  const out: FieldDiff[] = [];
  const add = (
    key: string,
    label: string,
    va: unknown,
    vb: unknown,
    language?: string,
  ) => {
    const before = a ? asText(va) : "",
      after = b ? asText(vb) : "";
    out.push({
      key,
      label,
      before,
      after,
      language,
      changed: before !== after,
    });
  };
  add("__name", "Name", a?.name, b?.name);
  add(
    "__disabled",
    "Disabled",
    a ? !!a.disabled : undefined,
    b ? !!b.disabled : undefined,
  );
  const order = (k: string) => {
    const i = specProps.findIndex((p) => p.name === k);
    return i < 0 ? 1e6 : i;
  };
  const keys = [...new Set([...Object.keys(pa), ...Object.keys(pb)])].sort(
    (x, y) => order(x) - order(y) || x.localeCompare(y),
  );
  for (const k of keys) {
    const ps = props.get(k);
    const v = pb[k] ?? pa[k];
    const language =
      ps?.type === "code"
        ? (ps.language ?? (k === "code" ? lang : undefined))
        : ps?.type === "json" || (typeof v === "object" && v !== null)
          ? "json"
          : undefined;
    add(k, ps?.label ?? k, pa[k], pb[k], language);
  }
  add("__settings", "Settings", a?.settings ?? {}, b?.settings ?? {}, "json");
  return out;
}

export function VersionCompare({
  wf,
  versions,
  draft,
  initial,
  readOnly,
  onClose,
  onRestore,
  onPublished,
}: {
  wf: Workflow;
  versions: WorkflowVersion[];
  /** The editor's current graph. */
  draft: Graph;
  initial: [string, string];
  readOnly: boolean;
  onClose: () => void;
  onRestore: (v: WorkflowVersion) => void;
  onPublished: (w: Workflow) => void;
}) {
  const [[base, target], setPair] = useState<[string, string]>(initial);
  const [graphs, setGraphs] = useState<Record<string, Graph>>({
    [DRAFT]: draft,
  });
  const [sel, setSel] = useState<string | null>(null);
  const [sync, setSync] = useState(true);
  const [bigDiff, setBigDiff] = useState(false); // hide the canvases, give the diff the whole height
  const [showSame, setShowSame] = useState(false);
  const [liveId, setLiveId] = useState(wf.published_version_id ?? null);
  const flows = useRef<{ a?: ReactFlowInstance; b?: ReactFlowInstance }>({});
  const mirroring = useRef<"a" | "b" | null>(null); // the canvas being moved by sync, so it doesn't echo back
  const { byType } = useCatalog();

  useEffect(() => {
    for (const id of [base, target]) {
      if (graphs[id]) continue;
      api
        .workflowVersion(wf.id, id)
        .then((v) =>
          setGraphs((g) => ({
            ...g,
            [id]: { nodes: v.nodes ?? [], edges: v.edges ?? [] },
          })),
        )
        .catch((e) => toast(String(e), "error"));
    }
  }, [base, target, graphs, wf.id]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !e.defaultPrevented) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const label = (id: string) => {
    if (id === DRAFT) return "Draft (editor)";
    const v = versions.find((x) => x.id === id);
    return v ? `v${v.number}${v.id === liveId ? " · Live" : ""}` : "Version";
  };
  const options = [
    { value: DRAFT, label: "Draft (editor)" },
    ...versions.map((v) => ({
      value: v.id,
      label: `v${v.number}${v.id === liveId ? " · Live" : ""} · ${new Date(v.created_at).toLocaleString()}`,
    })),
  ];
  const A = graphs[base],
    B = graphs[target];
  const diff = useMemo(() => (A && B ? diffGraphs(A, B) : null), [A, B]);
  const kindOf = useMemo(
    () => new Map((diff?.nodes ?? []).map((c) => [c.id, c.kind])),
    [diff],
  );

  const canvas = (g: Graph, side: "a" | "b") => {
    const nodes = g.nodes.map((n) => {
      const k = kindOf.get(n.id);
      const show =
        side === "a"
          ? k === "removed" || k === "changed"
          : k === "added" || k === "changed";
      return {
        ...toRF(n),
        className: [
          show ? `diff-${k}` : "diff-same",
          sel === n.id ? "diff-focus" : "",
        ].join(" "),
        selectable: true,
      };
    });
    const mark = side === "a" ? diff?.removedEdges : diff?.addedEdges;
    const edges = g.edges.map((e) => {
      const r = edgeToRF(e);
      return {
        ...r,
        className: [
          r.className,
          mark?.has(edgeKey(e))
            ? side === "a"
              ? "diff-removed"
              : "diff-added"
            : "",
        ]
          .filter(Boolean)
          .join(" "),
      };
    });
    const other = side === "a" ? "b" : "a";
    return (
      <ReactFlowProvider>
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          edgeTypes={edgeTypes}
          nodesDraggable={false}
          nodesConnectable={false}
          elementsSelectable
          zoomOnDoubleClick={false}
          minZoom={0.1}
          proOptions={{ hideAttribution: true }}
          onInit={(i) => {
            flows.current[side] = i as unknown as ReactFlowInstance;
          }}
          onNodeClick={(_, n) => setSel(n.id)}
          onMove={(ev: unknown, vp: Viewport) => {
            // Mirror only the user's own pan / zoom: fit-view and zoom-to-node run on each canvas separately
            // (mirroring those made the two canvases copy each other and neither could fit).
            const src = ev instanceof Event ? ev : (ev as { sourceEvent?: unknown } | null)?.sourceEvent;
            if (!sync || !(src instanceof Event) || mirroring.current === side) return;
            mirroring.current = other;
            flows.current[other]?.setViewport(vp);
            mirroring.current = null;
          }}
        >
          <Background
            variant={BackgroundVariant.Dots}
            gap={20}
            size={1.4}
            color="var(--canvas-dot)"
          />
          <Controls showInteractive={false} orientation="horizontal" />
          <FitWhenReady fitKey={side === "a" ? base : target} />
        </ReactFlow>
      </ReactFlowProvider>
    );
  };

  const focus = (id: string) => {
    setSel(id);
    for (const f of [flows.current.a, flows.current.b]) {
      if (f?.getNode(id))
        f.fitView({
          nodes: [{ id }],
          maxZoom: 1.1,
          duration: 300,
          padding: 0.6,
        });
    }
  };
  const focusRef = useRef(focus);
  focusRef.current = focus;

  const selected = sel ? (diff?.nodes.find((c) => c.id === sel) ?? null) : null;
  const selA = sel ? A?.nodes.find((n) => n.id === sel) : undefined;
  const selB = sel ? B?.nodes.find((n) => n.id === sel) : undefined;
  const fields = useMemo(
    () =>
      sel && (selA || selB)
        ? fieldDiffs(selA, selB, byType[(selB ?? selA)!.type])
        : [],
    [sel, selA, selB, byType],
  );
  const changedFields = fields.filter((f) => f.changed);
  const sameFields = fields.filter(
    (f) => !f.changed && !f.key.startsWith("__"),
  );
  // Open on the first change, so the diff shows something right away.
  // Keyed on the diff only: setSel re-renders, and a cleanup on `sel` would cancel the zoom.
  useEffect(() => {
    if (!diff) return;
    const id = diff.nodes[0]?.id;
    if (id) setSel((s) => s ?? id);
    // After both canvases have measured their nodes (fitView on mount runs before that).
    const t = window.setTimeout(() => {
      if (id) focusRef.current(id);
      else for (const f of [flows.current.a, flows.current.b]) f?.fitView({ padding: 0.2, maxZoom: 1, duration: 200 });
    }, 350);
    return () => window.clearTimeout(t);
  }, [diff]);

  const targetVersion = versions.find((v) => v.id === target);
  const publishTarget = async () => {
    if (!targetVersion) return;
    try {
      const saved = await api.publishVersion(wf.id, targetVersion.id);
      setLiveId(saved.published_version_id ?? null);
      onPublished(saved);
      toast(`v${targetVersion.number} is live`);
    } catch (e) {
      toast(String(e), "error");
    }
  };
  const count = (k: Kind) =>
    diff?.nodes.filter((c) => c.kind === k).length ?? 0;
  const total = (diff?.nodes.length ?? 0) + (diff?.edges.length ?? 0);

  return (
    <div
      className={`vc-shell ${bigDiff ? "big-diff" : ""}`}
      role="dialog"
      aria-label="Compare versions"
    >
      <div className="vc-head">
        <Icon name="git-compare" />
        <h2>Compare versions</h2>
        <div className="vc-pick">
          <Select
            value={base}
            onChange={(v) => {
              setPair([v, target]);
              setSel(null);
            }}
            options={options}
            title="Before"
          />
          <button
            className="btn ghost sm icon"
            title="Swap"
            onClick={() => {
              setPair([target, base]);
              setSel(null);
            }}
          >
            <Icon name="arrow-left-right" size={15} />
          </button>
          <Select
            value={target}
            onChange={(v) => {
              setPair([base, v]);
              setSel(null);
            }}
            options={options}
            title="After"
          />
        </div>
        {diff && (
          <div className="vc-sum">
            {total === 0 ? (
              <span className="badge plain">No changes</span>
            ) : (
              <>
                {count("changed") > 0 && (
                  <span className="vc-chip changed">
                    {count("changed")} changed
                  </span>
                )}
                {count("added") > 0 && (
                  <span className="vc-chip added">{count("added")} added</span>
                )}
                {count("removed") > 0 && (
                  <span className="vc-chip removed">
                    {count("removed")} removed
                  </span>
                )}
                {diff.edges.length > 0 && (
                  <span className="vc-chip conn">
                    {diff.edges.length} connection
                    {diff.edges.length === 1 ? "" : "s"}
                  </span>
                )}
              </>
            )}
          </div>
        )}
        <span className="spacer" />
        <label className="vc-sync" title="Pan and zoom both canvases together">
          <input
            type="checkbox"
            checked={sync}
            onChange={(e) => setSync(e.target.checked)}
          />{" "}
          Sync view
        </label>
        {!readOnly && targetVersion && (
          <>
            <button
              className="btn sm"
              onClick={() => onRestore(targetVersion)}
              title="Load this version into the editor"
            >
              <Icon name="rotate-ccw" size={14} />{" "}
              <span className="btn-text">Restore v{targetVersion.number}</span>
            </button>
            {targetVersion.id !== liveId && (
              <button
                className="btn sm primary"
                onClick={publishTarget}
                title="Production runs this version"
              >
                <Icon name="upload" size={14} />{" "}
                <span className="btn-text">
                  Publish v{targetVersion.number}
                </span>
              </button>
            )}
          </>
        )}
        <button
          className="btn ghost icon"
          onClick={onClose}
          aria-label="Close"
          title="Close (Esc)"
        >
          <Icon name="x" />
        </button>
      </div>
      <div className="vc-canvases">
        <div className="vc-pane">
          <div className="vc-pane-label">
            <span className="vc-dot removed" /> Before · <b>{label(base)}</b>
          </div>
          <div className="canvas vc-canvas">
            {A ? canvas(A, "a") : <div className="dv-empty">Loading…</div>}
          </div>
        </div>
        <div className="vc-pane">
          <div className="vc-pane-label">
            <span className="vc-dot added" /> After · <b>{label(target)}</b>
          </div>
          <div className="canvas vc-canvas">
            {B ? canvas(B, "b") : <div className="dv-empty">Loading…</div>}
          </div>
        </div>
      </div>
      <div className="vc-bottom">
        <div className="vc-list">
          {!diff ? (
            <div className="hint">Loading…</div>
          ) : total === 0 ? (
            <div className="hint">
              These two are identical
              {diff.moved
                ? ` (${diff.moved} node${diff.moved === 1 ? "" : "s"} only moved)`
                : ""}
              .
            </div>
          ) : (
            <>
              {diff.nodes.map((c) => (
                <button
                  key={c.id}
                  className={`vc-item ${c.kind} ${sel === c.id ? "on" : ""}`}
                  onClick={() => focus(c.id)}
                >
                  <span className={`vc-tag ${c.kind}`}>
                    {c.kind === "changed"
                      ? "M"
                      : c.kind === "added"
                        ? "+"
                        : "−"}
                  </span>
                  <Icon name={byType[c.type]?.icon ?? "box"} size={14} />
                  <span className="vc-item-name">{c.name}</span>
                  {c.fields.length > 0 && (
                    <span className="vc-fields">
                      {c.fields.slice(0, 4).join(", ")}
                      {c.fields.length > 4 ? ` +${c.fields.length - 4}` : ""}
                    </span>
                  )}
                </button>
              ))}
              {diff.edges.map((e) => (
                <div key={e.kind + e.key} className={`vc-item conn ${e.kind}`}>
                  <span className={`vc-tag ${e.kind}`}>
                    {e.kind === "added" ? "+" : "−"}
                  </span>
                  <Icon name="cable" size={14} />
                  <span className="vc-item-name">{e.label}</span>
                </div>
              ))}
              {diff.moved > 0 && (
                <div className="hint vc-moved">
                  {diff.moved} node{diff.moved === 1 ? "" : "s"} only moved (not
                  counted)
                </div>
              )}
            </>
          )}
        </div>
        <div className="vc-detail">
          {!sel ? (
            <div className="hint">
              Select a change (or a node on either canvas) to see what changed
              in it.
            </div>
          ) : (
            <>
              <div className="vc-detail-head">
                <b>{selB?.name ?? selA?.name}</b>
                {selected ? (
                  <span className={`vc-chip ${selected.kind}`}>
                    {selected.kind}
                  </span>
                ) : (
                  <span className="badge plain">unchanged</span>
                )}
                <span className="spacer" />
                <span className="vc-cols hide-sm">
                  <span>
                    <span className="vc-dot removed" /> {label(base)}
                  </span>
                  <span>
                    <span className="vc-dot added" /> {label(target)}
                  </span>
                </span>
                <button
                  className="btn ghost sm icon"
                  title={bigDiff ? "Show the canvases" : "Enlarge the diff"}
                  onClick={() => setBigDiff(!bigDiff)}
                >
                  <Icon
                    name={bigDiff ? "minimize-2" : "maximize-2"}
                    size={14}
                  />
                </button>
              </div>
              {changedFields.length === 0 && (
                <div className="hint">
                  No configuration changes in this node (it may only have
                  moved).
                </div>
              )}
              {changedFields.map((f) => (
                <section key={f.key} className="vc-field">
                  <div className="vc-field-head">
                    <Icon
                      name={
                        f.language && f.language !== "json"
                          ? "code"
                          : "sliders-horizontal"
                      }
                      size={13}
                    />{" "}
                    {f.label}
                    {f.language && (
                      <span className="vc-lang">{f.language}</span>
                    )}
                  </div>
                  <CodeDiff
                    before={f.before}
                    after={f.after}
                    language={f.language}
                  />
                </section>
              ))}
              {sameFields.length > 0 && (
                <button
                  className="vc-same"
                  onClick={() => setShowSame(!showSame)}
                >
                  <Icon
                    name={showSame ? "chevron-down" : "chevron-right"}
                    size={13}
                  />{" "}
                  {sameFields.length} unchanged parameter
                  {sameFields.length === 1 ? "" : "s"}
                  {!showSame && (
                    <span>: {sameFields.map((f) => f.label).join(", ")}</span>
                  )}
                </button>
              )}
              {showSame &&
                sameFields.map((f) => (
                  <section key={f.key} className="vc-field same">
                    <div className="vc-field-head">{f.label}</div>
                    <CodeDiff
                      before={f.before}
                      after={f.after}
                      language={f.language}
                    />
                  </section>
                ))}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
