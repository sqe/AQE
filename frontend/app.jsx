const { useEffect, useMemo, useState } = React;

const API = {
  workflows: "/api/workflows/v1/qe-runs",
  diagnostics: "/api/diagnostics/v1/diagnostics",
  heal: "/api/diagnostics/v1/diagnostics/heal",
  probes: "/api/diagnostics/v1/agent-probes",
  graph: "/api/diagnostics/v1/graph",
};

function Stamp({ state }) {
  return <span className={`stamp ${state}`}>[{state.toUpperCase()}]</span>;
}

function Panel({ index, title, action, children }) {
  return (
    <section className="panel">
      <header className="panel-title">
        <span><b>{index}</b> // {title}</span>
        {action}
      </header>
      {children}
    </section>
  );
}

function TopologyGraph({ graph, selected, onSelect }) {
  const groups = { agent: 0, target_agent: 1, orchestrator: 2, generated_test: 2, knowledge: 3, evidence: 3, state: 3, catalog: 3 };
  const columns = [150, 365, 580, 820];
  const grouped = graph.nodes.reduce((result, node) => {
    const column = groups[node.type] ?? 1;
    (result[column] ||= []).push(node);
    return result;
  }, {});
  const positions = {};
  Object.entries(grouped).forEach(([column, nodes]) => nodes.forEach((node, index) => {
    positions[node.id] = { x: columns[column], y: 50 + ((index + 1) * 380) / (nodes.length + 1) };
  }));
  const visibleEdges = graph.edges.filter((edge) => positions[edge.source] && positions[edge.target]);
  const selectedEdges = selected ? graph.edges.filter((edge) => edge.source === selected.id || edge.target === selected.id) : [];
  return (
    <div className="graph-layout">
      <svg className="topology" viewBox="0 0 960 480" role="img" aria-label="Live AQE agent interaction graph">
        <defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto"><path d="M0,0 L0,6 L8,3 z" /></marker></defs>
        {visibleEdges.map((edge, index) => {
          const source = positions[edge.source]; const target = positions[edge.target];
          return <line key={`${edge.source}-${edge.target}-${index}`} className={edge.type === "generated" ? "edge live" : "edge"} x1={source.x} y1={source.y} x2={target.x} y2={target.y}><title>{edge.type}: {edge.source} → {edge.target}</title></line>;
        })}
        {graph.nodes.map((node) => {
          const point = positions[node.id]; if (!point) return null;
          return <g key={node.id} className={`graph-node ${node.type} ${selected?.id === node.id ? "selected" : ""}`} transform={`translate(${point.x},${point.y})`} onClick={() => onSelect(node)} onKeyDown={(event) => event.key === "Enter" && onSelect(node)} tabIndex="0" role="button">
            <circle r={node.type === "generated_test" ? 9 : 13} /><text y="-20">{node.name}</text><text className="node-type" y="30">{node.type}</text>
          </g>;
        })}
      </svg>
      <aside className="node-detail">
        <small>SELECTED ENTITY</small>
        {selected ? <><h3>{selected.name}</h3><dl><dt>TYPE</dt><dd>{selected.type}</dd><dt>STATUS</dt><dd>{selected.status || "n/a"}</dd><dt>VERSION</dt><dd>{selected.version || "n/a"}</dd><dt>ONTOLOGY</dt><dd>{selected.archetype || "unclassified"}</dd><dt>SKILLS</dt><dd>{(selected.skills || []).join(", ") || "n/a"}</dd><dt>INTERACTIONS</dt><dd>{selectedEdges.map((edge) => `${edge.type} → ${edge.source === selected.id ? edge.target : edge.source}`).join(" | ") || "n/a"}</dd></dl></> : <p>select a node to inspect its contract and interactions_</p>}
      </aside>
    </div>
  );
}

function App() {
  const [diagnostics, setDiagnostics] = useState({ status: "checking", healthy: 0, total: 0, agents: [] });
  const [run, setRun] = useState({ url: "", spec: "", test_type: "agent", source_repository: "", source_ref: "", repair: true });
  const [runResult, setRunResult] = useState(null);
  const [probe, setProbe] = useState({ card_url: "", expected_skills: "" });
  const [probeResult, setProbeResult] = useState(null);
  const [busy, setBusy] = useState("");
  const [events, setEvents] = useState(["console initialized", "waiting for automaton input"]);
  const [graph, setGraph] = useState({ nodes: [], edges: [], events: [], stats: { node_count: 0, edge_count: 0 } });
  const [selectedNode, setSelectedNode] = useState(null);

  const log = (message) => setEvents((current) => [message, ...current].slice(0, 8));

  const request = async (url, options = {}) => {
    const response = await fetch(url, options);
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || payload.error || `HTTP ${response.status}`);
    return payload;
  };

  const scan = async (heal = false) => {
    setBusy("scan");
    try {
      const result = await request(heal ? API.heal : API.diagnostics, heal ? { method: "POST" } : {});
      setDiagnostics(result);
      log(`${heal ? "safe-heal" : "scan"}: ${result.healthy}/${result.total} agents healthy`);
    } catch (error) {
      setDiagnostics((current) => ({ ...current, status: "offline" }));
      log(`diagnostics unavailable: ${error.message}`);
    } finally {
      setBusy("");
    }
  };

  useEffect(() => {
    scan();
    const refreshGraph = () => request(API.graph).then(setGraph).catch((error) => log(`graph unavailable: ${error.message}`));
    refreshGraph();
    const timer = setInterval(() => scan(), 15000);
    const graphTimer = setInterval(refreshGraph, 5000);
    return () => { clearInterval(timer); clearInterval(graphTimer); };
  }, []);

  const startRun = async (event) => {
    event.preventDefault();
    setBusy("run");
    setRunResult(null);
    try {
      const result = await request(API.workflows, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(run),
      });
      setRunResult(result);
      log(`workflow accepted: ${result.workflow_id}`);
    } catch (error) {
      setRunResult({ error: error.message });
      log(`workflow rejected: ${error.message}`);
    } finally {
      setBusy("");
    }
  };

  const testAgent = async (event) => {
    event.preventDefault();
    setBusy("probe");
    setProbeResult(null);
    try {
      const expected_skills = probe.expected_skills.split(",").map((item) => item.trim()).filter(Boolean);
      const result = await request(API.probes, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ card_url: probe.card_url, expected_skills }),
      });
      setProbeResult(result);
      log(`agent probe: ${result.name} ${result.status}`);
    } catch (error) {
      setProbeResult({ status: "unhealthy", recommendation: error.message });
      log(`agent probe failed: ${error.message}`);
    } finally {
      setBusy("");
    }
  };

  const systemState = useMemo(() => {
    if (diagnostics.status === "healthy") return "healthy";
    if (diagnostics.status === "checking") return "checking";
    return "degraded";
  }, [diagnostics.status]);

  return (
    <main className="shell">
      <header className="masthead">
        <div>
          <p className="eyebrow">AUTONOMOUS QUALITY ENGINEERING / CONTROL PLANE</p>
          <h1><span>aqe</span>::automata</h1>
        </div>
        <div className="system-state">
          <small>SYSTEM STATE</small>
          <Stamp state={systemState} />
        </div>
      </header>

      <div className="rule"><span>GENERATE</span><i /><span>EXECUTE</span><i /><span>DIAGNOSE</span><i /><span>REPAIR</span></div>

      <div className="grid primary-grid">
        <Panel index="01" title="START QE AUTOMATON">
          <form onSubmit={startRun}>
            <label>TARGET URL</label>
            <input type="url" required placeholder="https://system-under-test.example" value={run.url} onChange={(event) => setRun({ ...run, url: event.target.value })} />
            <label>TEST RUNTIME</label>
            <select value={run.test_type} onChange={(event) => setRun({ ...run, test_type: event.target.value })}>
              <option value="agent">AGENT / HTTP + AGENT CARD</option>
              <option value="website">WEBSITE / PLAYWRIGHT BROWSER</option>
            </select>
            <label>GITHUB SOURCE <em>optional owner/repo + commit</em></label>
            <div className="inline-fields">
              <input placeholder="owner/agent-repository" value={run.source_repository} onChange={(event) => setRun({ ...run, source_repository: event.target.value })} />
              <input placeholder="commit SHA / tag" required={Boolean(run.source_repository)} value={run.source_ref} onChange={(event) => setRun({ ...run, source_ref: event.target.value })} />
            </div>
            <label>OBSERVABLE BEHAVIOR</label>
            <textarea required rows="5" placeholder="Describe the user journey and expected outcome." value={run.spec} onChange={(event) => setRun({ ...run, spec: event.target.value })} />
            <div className="command-row">
              <label className="toggle"><input type="checkbox" checked={run.repair} onChange={(event) => setRun({ ...run, repair: event.target.checked })} /> REPAIR LOOP</label>
              <button disabled={busy === "run"}>{busy === "run" ? "DISPATCHING..." : "RUN AUTOMATON →"}</button>
            </div>
          </form>
          {runResult && <pre className="result">{JSON.stringify(runResult, null, 2)}</pre>}
        </Panel>

        <Panel index="02" title="AGENT FLEET" action={<button className="ghost" onClick={() => scan(true)} disabled={busy === "scan"}>SAFE HEAL</button>}>
          <div className="fleet-summary"><strong>{diagnostics.healthy}/{diagnostics.total}</strong><span>contracts online</span></div>
          <div className="agent-list">
            {diagnostics.agents.length === 0 && <p className="empty">diagnostic agent is waiting for a fleet response_</p>}
            {diagnostics.agents.map((agent) => (
              <article className="agent-row" key={agent.name}>
                <div><b>{agent.name}</b><small>{agent.identity || "no identity"}</small></div>
                <Stamp state={agent.status} />
                {agent.recommendation && <p>{agent.recommendation}</p>}
              </article>
            ))}
          </div>
        </Panel>
      </div>

      <Panel index="03" title="LIVE AGENT + TEST GRAPH" action={<span className="graph-stats">{graph.stats.node_count}N / {graph.stats.edge_count}E // POLL 5S</span>}>
        <TopologyGraph graph={graph} selected={selectedNode} onSelect={setSelectedNode} />
      </Panel>

      <div className="grid secondary-grid">
        <Panel index="04" title="PROBE ANY AGENT">
          <form onSubmit={testAgent}>
            <label>AGENT CARD URL</label>
            <input type="url" required placeholder="https://agent.example/.well-known/agent.json" value={probe.card_url} onChange={(event) => setProbe({ ...probe, card_url: event.target.value })} />
            <label>EXPECTED SKILLS <em>comma separated</em></label>
            <input placeholder="research.run, report.generate" value={probe.expected_skills} onChange={(event) => setProbe({ ...probe, expected_skills: event.target.value })} />
            <button disabled={busy === "probe"}>{busy === "probe" ? "PROBING..." : "VALIDATE CONTRACT →"}</button>
          </form>
          {probeResult && <pre className="result">{JSON.stringify(probeResult, null, 2)}</pre>}
        </Panel>

        <Panel index="05" title="EVENT STREAM">
          <ol className="events">
            {events.map((event, index) => <li key={`${event}-${index}`}><time>{String(index).padStart(2, "0")}</time><span>{event}</span></li>)}
          </ol>
        </Panel>
      </div>

      <footer><span>AQE/2.0</span><span>Temporal durable state</span><span>RustFS evidence store</span><span>one observable outcome / test</span></footer>
    </main>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
