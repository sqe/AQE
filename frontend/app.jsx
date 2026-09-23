const { useEffect, useMemo, useRef, useState } = React;

const API = {
  workflows: "/api/workflows/v1/qe-runs",
  diagnostics: "/api/diagnostics/v1/diagnostics",
  heal: "/api/diagnostics/v1/diagnostics/heal",
  probes: "/api/diagnostics/v1/agent-probes",
  discovery: "/api/diagnostics/v1/agent-discovery",
  graph: "/api/diagnostics/v1/graph",
  documents: "/api/knowledge/v1/documents",
  knowledgeStats: "/api/knowledge/v1/stats",
  github: "/api/github/v1/status",
  githubAgents: "/api/github/v1/agents/discover",
};

const RUN_EXAMPLES = {
  agent: {
    url: "http://aqe-byoa:8009",
    agent_card_url: "http://aqe-byoa:8009/.well-known/agent.json",
    spec: "Verify the Agent Card declares qe.run and the agent returns a protocol-valid response for that skill.",
    test_type: "agent",
    source_repository: "",
    source_ref: "",
    repair: true,
    knowledge_document_id: "",
    refined_requirements: [],
  },
  website: {
    url: "https://example.com",
    agent_card_url: "",
    spec: "Verify the page title is Example Domain.",
    test_type: "website",
    source_repository: "",
    source_ref: "",
    repair: true,
    knowledge_document_id: "",
    refined_requirements: [],
  },
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

function githubRepositoryUrl(repository) {
  if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repository || "")) return null;
  return `https://github.com/${repository.split("/").map(encodeURIComponent).join("/")}`;
}

function ontologyDisplay(node) {
  if (node.ontology_product_name) return `${node.ontology_product_name} / ${node.ontology_label || node.archetype}`;
  return node.ontology_label || node.archetype || "unclassified";
}

const GRAPH_STAGES = ["CONTROL", "CONTEXT + GENERATION", "EXECUTION", "TARGETS + EVIDENCE"];

function graphDisplayName(node) {
  return node.name === "aqe" ? node.id.replace(/^(agent|target):/, "aqe-") : (node.name || node.id);
}

function stableTopology(graph) {
  const nodes = graph.nodes || [];
  const identityKey = (value) => String(value || "").toLowerCase().replace(/[^a-z0-9]/g, "");
  const agentNames = new Set(nodes
    .filter((node) => node.type === "agent")
    .flatMap((node) => [identityKey(node.name), identityKey(graphDisplayName(node))]));
  const workflowNodes = nodes.filter((node) => node.type === "workflow" && node.id !== "workflow:contract-check");
  const stableNodes = nodes.filter((node) => {
    if (["workflow", "generated_test"].includes(node.type)) return false;
    if (node.type === "target_agent" && agentNames.has(identityKey(node.name))) return false;
    return true;
  });
  if (workflowNodes.length) {
    const activeCampaigns = new Set(workflowNodes.map((node) => node.id
      .replace(/^workflow:/, "")
      .replace(/-agent-\d+$/, "")));
    stableNodes.push({
      id: "campaign:active-fleet",
      name: activeCampaigns.size === 1 ? "ACTIVE QE CAMPAIGN" : `${activeCampaigns.size} ACTIVE QE CAMPAIGNS`,
      type: "orchestrator",
      status: workflowNodes.some((node) => node.status === "running") ? "running" : "observed",
      summary: { instances: workflowNodes.length, campaigns: activeCampaigns.size },
    });
  }
  const visibleIds = new Set(stableNodes.map((node) => node.id));
  const edges = (graph.edges || []).filter((edge) => visibleIds.has(edge.source) && visibleIds.has(edge.target));
  if (visibleIds.has("campaign:active-fleet") && visibleIds.has("system:temporal")) {
    edges.push({ source: "system:temporal", target: "campaign:active-fleet", type: "orchestrates" });
  }
  const uniqueEdges = [...new Map(edges.map((edge) => [`${edge.source}|${edge.target}|${edge.type}`, edge])).values()];
  return {
    ...graph,
    nodes: stableNodes,
    edges: uniqueEdges,
    stats: { node_count: stableNodes.length, edge_count: uniqueEdges.length },
  };
}

function stableActivityEdges(event) {
  const declared = event?.edges || [];
  if (!event?.kind) return declared;
  const executor = event.test_type === "website" ? "agent:website-execution" : "agent:test-execution";
  const lifecycleRoute = {
    test_generated: [{ source: "agent:test-generation", target: "agent:quality-oracle" }],
    test_started: [{ source: "agent:quality-oracle", target: executor }],
    test_completed: [{ source: executor, target: event.status === "passed" ? "catalog:github" : "store:postgres" }],
  }[event.kind];
  return lifecycleRoute || declared;
}

function graphStageFor(node) {
  if (node.type === "agent_family") return 0;
  if (["target_agent", "knowledge", "evidence", "state", "catalog"].includes(node.type)) return 3;
  if (node.type === "generated_test" || /test-execution|website-execution|webpage-state/.test(node.id)) return 2;
  if (/test-generation|quality-oracle|knowledge-ingestion|github-analysis|github-connector|agent-builder|dbt-builder/.test(node.id)) return 1;
  if (/artifact-management|github-commit/.test(node.id)) return 3;
  return 0;
}

function TopologyGraph({ graph, selected, onSelect, github }) {
  const stages = GRAPH_STAGES.map((name, index) => ({ name, x: 105 + index * 240 }));
  const grouped = graph.nodes.reduce((result, node) => {
    const column = graphStageFor(node);
    (result[column] ||= []).push(node);
    return result;
  }, {});
  const rowHeight = 48;
  const canvasHeight = Math.max(650, 90 + Math.max(0, ...Object.values(grouped).map((nodes) => nodes.length)) * rowHeight);
  const positions = {};
  Object.entries(grouped).forEach(([column, nodes]) => nodes
    .sort((left, right) => left.name.localeCompare(right.name))
    .forEach((node, index) => {
    positions[node.id] = {
      x: stages[column].x,
      y: 65 + index * rowHeight,
      labelSide: 1,
    };
  }));
  const selectedEdges = selected ? graph.edges.filter((edge) => edge.source === selected.id || edge.target === selected.id) : [];
  const visibleEdges = graph.edges.filter(
    (edge) => positions[edge.source] && positions[edge.target] && (!selected || selectedEdges.includes(edge))
  );
  const latestEvent = graph.events?.[0];
  const activeEdges = new Set((latestEvent?.edges || []).map((edge) => `${edge.source}|${edge.target}|${edge.type}`));
  const catalogRepositoryUrl = githubRepositoryUrl(github?.catalog_repository);
  const catalogUrl = catalogRepositoryUrl && github?.catalog_branch
    ? `${catalogRepositoryUrl}/tree/${encodeURIComponent(github.catalog_branch)}/generated-tests`
    : catalogRepositoryUrl;
  return (
    <div className="graph-layout">
      <svg className="topology" style={{ height: `${canvasHeight}px` }} viewBox={`0 0 960 ${canvasHeight}`} role="img" aria-label="Live AQE agent interaction graph">
        <defs>
          <marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto"><path d="M0,0 L0,6 L8,3 z" /></marker>
          <filter id="glow"><feGaussianBlur stdDeviation="2.5" result="blur" /><feMerge><feMergeNode in="blur" /><feMergeNode in="SourceGraphic" /></feMerge></filter>
        </defs>
        {stages.map((stage, index) => <g key={stage.name} className="graph-stage"><text x={stage.x} y="22">0{index + 1} / {stage.name}</text><line x1={stage.x} y1="32" x2={stage.x} y2={canvasHeight - 20} /></g>)}
        {visibleEdges.map((edge, index) => {
          const source = positions[edge.source]; const target = positions[edge.target];
          const key = `${edge.source}|${edge.target}|${edge.type}`;
          const active = activeEdges.has(key);
          const highlighted = active || selectedEdges.includes(edge);
          const bend = source.x === target.x ? 55 : Math.max(45, Math.abs(target.x - source.x) * 0.35);
          const path = `M ${source.x} ${source.y} C ${source.x + bend} ${source.y}, ${target.x - bend} ${target.y}, ${target.x} ${target.y}`;
          return <g key={`${key}-${index}`} className={`edge-group ${active ? "active" : ""} ${highlighted ? "highlighted" : ""}`}>
            <path className="edge" d={path}><title>{edge.type}: {edge.source} → {edge.target}</title></path>
            {highlighted && <text className="edge-label" x={(source.x + target.x) / 2} y={(source.y + target.y) / 2 - 5}>{edge.type}</text>}
          </g>;
        })}
        {graph.nodes.map((node) => {
          const point = positions[node.id]; if (!point) return null;
          return <g key={node.id} className={`graph-node ${node.type} status-${node.status || "unknown"} ${selected?.id === node.id ? "selected" : ""}`} transform={`translate(${point.x},${point.y})`} onClick={() => onSelect(node)} onKeyDown={(event) => event.key === "Enter" && onSelect(node)} tabIndex="0" role="button">
            <circle className="node-halo" r="18" />
            <circle r={node.type === "generated_test" ? 9 : 12} />
            <text x="18" y="-4">{graphDisplayName(node).slice(0, 24)}<title>{graphDisplayName(node)}</title></text>
            <text className="node-type" x="18" y="10">{node.status || node.type}</text>
          </g>;
        })}
      </svg>
      <aside className="graph-sidebar">
        <div className="node-detail">
          <small>ENTITY INSPECTOR</small>
          {selected ? <><h3>{graphDisplayName(selected)}</h3><dl><dt>TYPE / STATUS</dt><dd>{selected.type} / {selected.status || "n/a"}</dd><dt>VERSION</dt><dd>{selected.version || "n/a"}</dd><dt>ONTOLOGY</dt><dd>{ontologyDisplay(selected)}</dd><dt>SKILLS</dt><dd>{(selected.skills || []).join(", ") || "n/a"}</dd><dt>RESULT</dt><dd>{Object.keys(selected.summary || {}).length ? JSON.stringify(selected.summary) : "n/a"}</dd>{selected.type === "catalog" && catalogUrl && <><dt>GITHUB</dt><dd><a href={catalogUrl} target="_blank" rel="noreferrer">OPEN GENERATED TEST CATALOG ↗</a></dd></>}<dt>DIRECTED INTERACTIONS</dt><dd>{selectedEdges.map((edge) => edge.source === selected.id ? `OUT → ${edge.target} [${edge.type}]` : `IN ← ${edge.source} [${edge.type}]`).join(" | ") || "n/a"}</dd></dl></> : <p>select a node to inspect its contract, state, and directed interactions_</p>}
        </div>
        <div className="activity-rail">
          <small>LIVE EXECUTION ACTIVITY</small>
          {(graph.events || []).slice(0, 7).map((event) => <button key={event.id} type="button" onClick={() => onSelect(graph.nodes.find((node) => node.id === (event.task_id ? `test:${event.task_id}` : `workflow:${event.workflow_id}`)))}>
            <time>{event.timestamp ? new Date(event.timestamp).toLocaleTimeString([], { hour12: false }) : "--:--:--"}</time>
            <b>{String(event.kind || "event").replaceAll("_", " ")}</b>
            <span>{event.test_type || "agent"} / {event.status || "received"} / {event.task_id || event.workflow_id || "n/a"}</span>
          </button>)}
          {!graph.events?.length && <p>waiting for a generated test_</p>}
        </div>
      </aside>
    </div>
  );
}

function Topology3D({ graph }) {
  const mountRef = useRef(null);
  const topologyGraph = useMemo(() => stableTopology(graph), [graph]);
  const graphRef = useRef(topologyGraph);
  const [selected, setSelected] = useState(null);

  useEffect(() => { graphRef.current = topologyGraph; }, [topologyGraph]);

  useEffect(() => {
    const THREE = window.THREE;
    const mount = mountRef.current;
    if (!THREE || !mount) return undefined;

    const scene = new THREE.Scene();
    scene.fog = new THREE.FogExp2(0x020604, 0.026);
    const camera = new THREE.PerspectiveCamera(48, 1, 0.1, 100);
    camera.position.set(0, 1.5, 19);
    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setClearColor(0x020604, 1);
    mount.appendChild(renderer.domElement);

    const root = new THREE.Group();
    scene.add(root);
    scene.add(new THREE.AmbientLight(0x58ff9a, 1.1));
    const light = new THREE.PointLight(0x7dffb1, 18, 40);
    light.position.set(2, 5, 8);
    scene.add(light);

    let nodeMeshes = [];
    let activeNodeMeshes = [];
    let flowParticles = [];
    let frame = 0;
    const raycaster = new THREE.Raycaster();
    const pointer = new THREE.Vector2();
    const drag = { active: false, moved: false, x: 0, y: 0 };

    const labelTexture = (text) => {
      const canvas = document.createElement("canvas");
      canvas.width = 512;
      canvas.height = 64;
      const context = canvas.getContext("2d");
      const label = text.slice(0, 34);
      context.font = `${Math.min(30, Math.max(19, 720 / Math.max(label.length, 1)))}px monospace`;
      context.textAlign = "center";
      context.shadowColor = "#020604";
      context.shadowBlur = 6;
      context.fillStyle = "rgba(2, 6, 4, 0.88)";
      context.fillRect(72, 8, 368, 46);
      context.fillStyle = "#b9f6cf";
      context.fillText(label, 256, 42);
      const texture = new THREE.CanvasTexture(canvas);
      texture.colorSpace = THREE.SRGBColorSpace;
      return texture;
    };

    const rebuild = () => {
      while (root.children.length) {
        const item = root.children.pop();
        item.traverse((child) => {
          child.geometry?.dispose();
          child.material?.map?.dispose();
          child.material?.dispose();
        });
      }
      nodeMeshes = [];
      activeNodeMeshes = [];
      flowParticles = [];
      const current = graphRef.current || { nodes: [], edges: [], events: [] };
      const positions = new Map();
      const stageX = [-7.5, -2.5, 2.5, 7.5];
      const grouped = current.nodes.reduce((result, node) => {
        (result[graphStageFor(node)] ||= []).push(node);
        return result;
      }, {});
      Object.entries(grouped).forEach(([stage, nodes]) => nodes
        .sort((left, right) => String(left.name).localeCompare(String(right.name)))
        .forEach((node, index) => {
          const spacing = Math.min(1.6, 8 / Math.max(nodes.length - 1, 1));
          positions.set(node.id, new THREE.Vector3(
            stageX[Number(stage)],
            ((nodes.length - 1) / 2 - index) * spacing,
            (index % 2 ? -0.35 : 0.35),
          ));
        }));
      GRAPH_STAGES.forEach((stage, index) => {
        const guide = new THREE.Line(
          new THREE.BufferGeometry().setFromPoints([
            new THREE.Vector3(stageX[index], -5.2, -0.6),
            new THREE.Vector3(stageX[index], 5.2, -0.6),
          ]),
          new THREE.LineBasicMaterial({ color: 0x123a20, transparent: true, opacity: 0.55 }),
        );
        root.add(guide);
        const label = new THREE.Sprite(new THREE.SpriteMaterial({ map: labelTexture(`0${index + 1} / ${stage}`), transparent: true }));
        label.position.set(stageX[index], 5.7, 0);
        label.scale.set(3.8, 0.48, 1);
        root.add(label);
      });

      const latest = current.events?.[0];
      const activeEdges = new Set(stableActivityEdges(latest).map((edge) => `${edge.source}|${edge.target}`));
      const activeNodeIds = new Set(stableActivityEdges(latest).flatMap((edge) => [edge.source, edge.target]));
      current.edges.forEach((edge) => {
        const start = positions.get(edge.source);
        const end = positions.get(edge.target);
        if (!start || !end) return;
        const active = activeEdges.has(`${edge.source}|${edge.target}`);
        const curve = new THREE.CatmullRomCurve3([
          start,
          start.clone().lerp(end, 0.5).add(new THREE.Vector3(0, 0, 1.2)),
          end,
        ]);
        root.add(new THREE.Mesh(
          new THREE.TubeGeometry(curve, 20, active ? 0.045 : 0.018, 5, false),
          new THREE.MeshBasicMaterial({ color: active ? 0x7dffb1 : 0x1a6038, transparent: true, opacity: active ? 0.92 : 0.5 }),
        ));
        const arrow = new THREE.Mesh(
          new THREE.ConeGeometry(active ? 0.13 : 0.09, active ? 0.42 : 0.3, 8),
          new THREE.MeshBasicMaterial({ color: active ? 0xb9f6cf : 0x3a8755 }),
        );
        arrow.position.copy(curve.getPoint(0.82));
        arrow.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), curve.getTangent(0.82).normalize());
        root.add(arrow);
        if (active) {
          const particle = new THREE.Mesh(
            new THREE.SphereGeometry(0.09, 8, 8),
            new THREE.MeshBasicMaterial({ color: 0xd6ffdc }),
          );
          particle.userData = { curve, offset: Math.random() };
          flowParticles.push(particle);
          root.add(particle);
        }
      });

      current.nodes.forEach((node) => {
        const active = activeNodeIds.has(node.id);
        const color = active || ["running", "queued"].includes(node.status)
          ? 0xffd166 : ["failed", "unreachable"].includes(node.status) ? 0xff5d72 : 0x5dff99;
        const mesh = new THREE.Mesh(
          new THREE.IcosahedronGeometry(node.type === "agent" ? 0.43 : 0.62, 1),
          new THREE.MeshStandardMaterial({ color, emissive: color, emissiveIntensity: active ? 0.8 : 0.22, roughness: 0.45 }),
        );
        mesh.position.copy(positions.get(node.id));
        mesh.userData = { ...node, topologyActive: active };
        nodeMeshes.push(mesh);
        if (active) activeNodeMeshes.push(mesh);
        root.add(mesh);
      });
    };

    const resize = () => {
      const { clientWidth, clientHeight } = mount;
      renderer.setSize(clientWidth, clientHeight, false);
      camera.aspect = clientWidth / Math.max(clientHeight, 1);
      camera.updateProjectionMatrix();
    };
    const onDown = (event) => Object.assign(drag, { active: true, moved: false, x: event.clientX, y: event.clientY });
    const onMove = (event) => {
      if (!drag.active) return;
      const dx = event.clientX - drag.x;
      const dy = event.clientY - drag.y;
      drag.moved ||= Math.abs(dx) + Math.abs(dy) > 3;
      root.rotation.y += dx * 0.007;
      root.rotation.x += dy * 0.004;
      Object.assign(drag, { x: event.clientX, y: event.clientY });
    };
    const onUp = (event) => {
      if (!drag.moved) {
        const rect = renderer.domElement.getBoundingClientRect();
        pointer.set(((event.clientX - rect.left) / rect.width) * 2 - 1, -((event.clientY - rect.top) / rect.height) * 2 + 1);
        raycaster.setFromCamera(pointer, camera);
        setSelected(raycaster.intersectObjects(nodeMeshes)[0]?.object.userData || null);
      }
      drag.active = false;
    };
    const onWheel = (event) => {
      event.preventDefault();
      camera.position.z = Math.max(8, Math.min(30, camera.position.z + event.deltaY * 0.012));
    };
    const animate = () => {
      frame = requestAnimationFrame(animate);
      const now = performance.now() / 1800;
      flowParticles.forEach((particle) => particle.position.copy(particle.userData.curve.getPoint((now + particle.userData.offset) % 1)));
      const activeScale = 1.08 + Math.sin(performance.now() / 220) * 0.12;
      activeNodeMeshes.forEach((mesh) => mesh.scale.setScalar(activeScale));
      renderer.render(scene, camera);
    };

    rebuild();
    resize();
    animate();
    window.addEventListener("resize", resize);
    renderer.domElement.addEventListener("pointerdown", onDown);
    renderer.domElement.addEventListener("pointermove", onMove);
    renderer.domElement.addEventListener("pointerup", onUp);
    renderer.domElement.addEventListener("pointerleave", onUp);
    renderer.domElement.addEventListener("wheel", onWheel, { passive: false });
    const refresh = window.setInterval(rebuild, 5000);

    return () => {
      window.clearInterval(refresh);
      cancelAnimationFrame(frame);
      window.removeEventListener("resize", resize);
      renderer.dispose();
      mount.removeChild(renderer.domElement);
    };
  }, []);

  const latest = topologyGraph.events?.[0];
  const selectedConnections = selected
    ? topologyGraph.edges.filter((edge) => edge.source === selected.id || edge.target === selected.id)
    : [];
  const stageNodes = GRAPH_STAGES.map((_, stage) => topologyGraph.nodes
    .filter((node) => graphStageFor(node) === stage)
    .sort((left, right) => graphDisplayName(left).localeCompare(graphDisplayName(right))));
  return (
    <>
    <div className="topology-3d-legend">
      {GRAPH_STAGES.map((stage, index) => <section key={stage}><small>0{index + 1} / {stage}</small>{stageNodes[index].map((node) => <button type="button" key={node.id} onClick={() => setSelected(node)}><i className={`node-dot status-${node.status || "unknown"}`} />{graphDisplayName(node)}</button>)}</section>)}
    </div>
    <div className="topology-3d-shell">
      <div className="topology-3d" ref={mountRef} />
      <div className="topology-hud"><span>ARROWS SOURCE → TARGET</span><span>DRAG ROTATE</span><span>SCROLL ZOOM</span><span>SELECT NODE</span></div>
      <aside className="topology-3d-inspector">
        <small>SELECTED ENTITY</small><strong>{selected ? graphDisplayName(selected) : "none"}</strong>
        <span>{selected ? `${selected.type || "agent"} / ${selected.status || "unknown"}` : "click a node to inspect"}</span>
        {selected && <><small>DIRECTED CONNECTIONS</small><span>{selectedConnections.map((edge) => edge.source === selected.id ? `OUT → ${edge.target} [${edge.type}]` : `IN ← ${edge.source} [${edge.type}]`).join(" · ") || "none"}</span></>}
        <small>LATEST ACTIVITY</small><strong>{latest?.kind || "idle"}</strong>
        <span>{latest?.agent_id || latest?.task_id || "awaiting workflow events"}</span>
      </aside>
    </div>
    </>
  );
}

function GithubStatus({ github }) {
  const repositories = github?.source_repositories || [];
  const connected = github?.status === "connected";
  const catalogRepositoryUrl = githubRepositoryUrl(github?.catalog_repository);
  const catalogUrl = catalogRepositoryUrl && github?.catalog_branch
    ? `${catalogRepositoryUrl}/tree/${encodeURIComponent(github.catalog_branch)}/generated-tests`
    : catalogRepositoryUrl;
  return (
    <section className="github-strip">
      <div className="github-identity"><i className={`status-light ${connected ? "ok" : "bad"}`} /><div><small>GITHUB MCP</small><strong>{github?.status || "checking"}</strong></div></div>
      <div><small>SOURCE REPOSITORIES</small><strong className="repository-links">{repositories.length ? repositories.map((repository, index) => <React.Fragment key={repository}>{index > 0 && " · "}<a href={githubRepositoryUrl(repository)} target="_blank" rel="noreferrer">{repository}</a></React.Fragment>) : "none allowlisted"}</strong></div>
      <div><small>GENERATED TEST CATALOG</small><strong>{catalogUrl ? <a href={catalogUrl} target="_blank" rel="noreferrer">{github.catalog_repository} / {github.catalog_branch} ↗</a> : "not configured"}</strong></div>
      <div><small>TOOLS</small><strong>{github?.discovered_tools?.length || 0} / {github?.enabled_tools?.length || 0} available</strong></div>
    </section>
  );
}

function ModelProviderStatus({ provider }) {
  const connected = provider?.status === "connected";
  return (
    <section className="github-strip" aria-label="Generation model connectivity">
      <div className="github-identity"><i className={`status-light ${connected ? "ok" : "bad"}`} /><div><small>GENERATION MODEL</small><strong>{provider?.mode || "checking"}</strong></div></div>
      <div><small>CONNECTIVITY</small><strong>{provider?.status || "checking"}</strong></div>
      <div><small>MODEL</small><strong>{provider?.model || "not configured"}</strong></div>
      <div><small>LATENCY</small><strong>{connected ? `${provider.latency_ms} ms` : "n/a"}</strong></div>
    </section>
  );
}

function KnowledgeStatus({ stats, graph }) {
  const stores = stats?.stores || {};
  const items = [
    ["RAG VECTORS", stores.qdrant_vectors],
    ["REQUIREMENTS", stores.rag_requirements],
    ["ONTOLOGY", stores.ontology_records],
    ["DOCUMENTS", stores.documents],
    ["EVIDENCE", stores.evidence_objects],
    ["TEST RUNS", stores.test_runs],
    ["ACTIVE VERSIONS", stores.active_versions],
    ["LIVE EVENTS", { status: "available", count: graph?.events?.length || 0 }],
  ];
  return <section className="knowledge-strip" aria-label="Knowledge and persistence counts">
    {items.map(([label, value]) => <div key={label}><small>{label}</small><strong className={value?.status === "available" ? "" : "unavailable"}>{value?.status === "available" ? value.count : "unavailable"}</strong></div>)}
  </section>;
}

function WorkflowHistory({ history }) {
  const namespace = history?.namespace || "default";
  const uiBase = history?.temporal_ui_url?.replace(/\/$/, "");
  const workflowUrl = (workflow) => uiBase
    ? `${uiBase}/namespaces/${encodeURIComponent(namespace)}/workflows/${encodeURIComponent(workflow.workflow_id)}/${encodeURIComponent(workflow.run_id)}/history`
    : null;
  return <div className="workflow-history">
    <header><div><small>TEMPORAL DURABLE HISTORY</small><strong>{history?.count ?? "unavailable"} workflows</strong></div>{uiBase && <a href={uiBase} target="_blank" rel="noreferrer">OPEN TEMPORAL UI ↗</a>}</header>
    <div className="workflow-history-list">
      {(history?.workflows || []).map((workflow) => <article key={workflow.run_id}>
        <time>{new Date(workflow.start_time).toLocaleString()}</time>
        <div><b>{workflow.workflow_id}</b><span>{workflow.parent_workflow_id ? `child of ${workflow.parent_workflow_id}` : workflow.workflow_type}</span></div>
        <span className={`stamp ${String(workflow.status).toLowerCase()}`}>[{workflow.status}]</span>
        <small>{workflow.history_length || workflow.status !== "RUNNING" ? `${workflow.history_length} EVENTS` : "ACTIVE / HISTORY SYNCING"}</small>
        {workflowUrl(workflow) && <a href={workflowUrl(workflow)} target="_blank" rel="noreferrer">HISTORY ↗</a>}
      </article>)}
      {!history?.workflows?.length && <p className="empty">no Temporal workflow summaries available_</p>}
    </div>
  </div>;
}

function CatalogLinks({ workflow }) {
  const direct = workflow?.result?.test_catalog ? [workflow.result.test_catalog] : [];
  const fleet = (workflow?.result?.runs || []).map((run) => run.result?.test_catalog).filter(Boolean);
  const catalogs = [...direct, ...fleet].filter(
    (catalog, index, all) => catalog?.url && all.findIndex((item) => item?.url === catalog.url) === index
  );
  if (!catalogs.length) return null;
  return <div className="catalog-links"><small>GITHUB TEST OUTPUTS</small>{catalogs.map((catalog) => <span key={catalog.url}><a href={catalog.url} target="_blank" rel="noreferrer">{catalog.path} ↗</a>{catalog.commit_url && <a href={catalog.commit_url} target="_blank" rel="noreferrer">commit ↗</a>}</span>)}</div>;
}

function App() {
  const [diagnostics, setDiagnostics] = useState({ status: "checking", healthy: 0, total: 0, agents: [] });
  const [run, setRun] = useState({ url: "", agent_card_url: "", spec: "", test_type: "agent", source_repository: "", source_ref: "", repair: true, knowledge_document_id: "", refined_requirements: [] });
  const [documentResult, setDocumentResult] = useState(null);
  const [runResult, setRunResult] = useState(null);
  const [probe, setProbe] = useState({ card_url: "", max_latency_ms: 5000, min_accuracy: 0.8 });
  const [probeResult, setProbeResult] = useState(null);
  const [autopilotResult, setAutopilotResult] = useState(null);
  const [replaceActiveCampaign, setReplaceActiveCampaign] = useState(false);
  const [busy, setBusy] = useState("");
  const [events, setEvents] = useState(["console initialized", "waiting for automaton input"]);
  const [graph, setGraph] = useState({ nodes: [], edges: [], events: [], stats: { node_count: 0, edge_count: 0 } });
  const [selectedNode, setSelectedNode] = useState(null);
  const [activeView, setActiveView] = useState("discover");
  const [observeMode, setObserveMode] = useState("graph");
  const [github, setGithub] = useState({ status: "checking", source_repositories: [], discovered_tools: [], enabled_tools: [] });
  const [knowledgeStats, setKnowledgeStats] = useState({ status: "checking", stores: {} });
  const [workflowHistory, setWorkflowHistory] = useState({ count: 0, workflows: [] });

  const log = (message) => setEvents((current) => [message, ...current].slice(0, 8));

  const request = async (url, options = {}) => {
    const response = await fetch(url, options);
    const body = await response.text();
    let payload = {};
    try {
      payload = body ? JSON.parse(body) : {};
    } catch (_error) {
      payload = { detail: body };
    }
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
    const refreshGithub = () => request(API.github).then(setGithub).catch((error) => setGithub((current) => ({ ...current, status: "unavailable", message: error.message })));
    const refreshKnowledge = () => request(API.knowledgeStats).then(setKnowledgeStats).catch(() => setKnowledgeStats({ status: "unavailable", stores: {} }));
    const refreshHistory = () => request(`${API.workflows}?limit=50`).then(setWorkflowHistory).catch(() => setWorkflowHistory({ count: null, workflows: [] }));
    refreshGraph();
    refreshGithub();
    refreshKnowledge();
    refreshHistory();
    const timer = setInterval(() => scan(), 15000);
    const graphTimer = setInterval(refreshGraph, 5000);
    const githubTimer = setInterval(refreshGithub, 30000);
    const knowledgeTimer = setInterval(refreshKnowledge, 30000);
    const historyTimer = setInterval(refreshHistory, 10000);
    return () => { clearInterval(timer); clearInterval(graphTimer); clearInterval(githubTimer); clearInterval(knowledgeTimer); clearInterval(historyTimer); };
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
      let lastStatus = result.status;
      for (let attempt = 0; attempt < 450; attempt += 1) {
        await new Promise((resolve) => setTimeout(resolve, 2000));
        const current = await request(`${API.workflows}/${result.workflow_id}`);
        setRunResult(current);
        if (current.status !== lastStatus) {
          log(`workflow ${result.workflow_id}: ${current.status}`);
          lastStatus = current.status;
        }
        if (["COMPLETED", "FAILED", "CANCELED", "TERMINATED", "TIMED_OUT"].includes(current.status)) break;
      }
    } catch (error) {
      setRunResult({ error: error.message });
      log(`workflow rejected: ${error.message}`);
    } finally {
      setBusy("");
    }
  };

  const uploadDocument = async (event) => {
    const file = event.target.files?.[0];
    if (!file) return;
    setBusy("document");
    setDocumentResult(null);
    try {
      const body = new FormData();
      body.append("file", file);
      const result = await request(API.documents, { method: "POST", body });
      setDocumentResult(result);
      setRun((current) => ({
        ...current,
        knowledge_document_id: result.document_id,
        refined_requirements: result.requirements,
        spec: current.spec || result.requirements.filter((item) => item.testable).map((item) => item.statement).join("\n"),
      }));
      log(`requirements ingested: ${result.summary.testable}/${result.summary.extracted} testable`);
    } catch (error) {
      setDocumentResult({ error: error.message });
      log(`document ingestion failed: ${error.message}`);
    } finally {
      setBusy("");
      event.target.value = "";
    }
  };

  const testAgent = async (event, discoverAll = false) => {
    event?.preventDefault();
    setBusy("probe");
    setProbeResult(null);
    try {
      const cardUrls = discoverAll || !probe.card_url.trim() ? [] : [probe.card_url.trim()];
      const result = await request(API.discovery, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ card_urls: cardUrls, max_depth: 2, max_latency_ms: Number(probe.max_latency_ms), min_accuracy: Number(probe.min_accuracy) }),
      });
      setProbeResult(result);
      log(`${cardUrls.length ? "agent" : "fleet"} discovery: ${result.summary.discovered} agents / ${result.summary.testable_scenarios} scenarios`);
    } catch (error) {
      setProbeResult({ status: "unhealthy", recommendation: error.message });
      log(`agent probe failed: ${error.message}`);
    } finally {
      setBusy("");
    }
  };

  const runAutopilot = async () => {
    setBusy("autopilot");
    setAutopilotResult({ status: "DISCOVERING", message: "mapping runtime fleet and connected repositories" });
    try {
      const [discovery, repositoryDiscovery] = await Promise.all([
        request(API.discovery, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ card_urls: [], max_depth: 2, max_agents: 50, max_latency_ms: Number(probe.max_latency_ms), min_accuracy: Number(probe.min_accuracy) }),
        }),
        request(API.githubAgents).catch((error) => ({ status: "unavailable", agents: [], errors: [{ error: error.message }] })),
      ]);
      const agents = discovery.agents.filter((agent) => agent.status === "healthy");
      if (!agents.length) throw new Error("no healthy configured agents were discovered");
      const normalizeName = (value) => String(value || "").toLowerCase().replace(/[^a-z0-9]/g, "").replace(/^aqe/, "");
      const matchedRepositoryAgents = new Set();
      const requirementsNeededAgents = [];
      const runs = agents.flatMap((agent) => {
        const cardUrl = agent.card_url;
        const baseUrl = agent.invocation_url || cardUrl.replace(/\/(agent_card|\.well-known\/agent\.json)\/?$/, "");
        const skills = agent.skills || [];
        const runtimeNames = [agent.identity, agent.name, agent.card?.name].map(normalizeName).filter(Boolean);
        const repositoryAgent = repositoryDiscovery.agents.find((candidate) => runtimeNames.includes(normalizeName(candidate.name)));
        if (repositoryAgent) matchedRepositoryAgents.add(`${repositoryAgent.repository}:${repositoryAgent.path}`);
        const scenarios = agent.scenarios || [];
        const executableScenarios = scenarios.filter((scenario) => scenario.execution_status === "executable");
        const contractGaps = scenarios.filter((scenario) => scenario.execution_status !== "executable");
        if (!executableScenarios.length) {
          requirementsNeededAgents.push({
            name: agent.identity || agent.name,
            card_url: cardUrl,
            scenarios: contractGaps,
          });
          return [];
        }
        const run = {
          url: baseUrl,
          agent_card_url: cardUrl,
          test_type: "agent",
          agent_name: agent.identity || agent.name,
          agent_version: agent.version || "unversioned",
          target_agent: {
            id: agent.identity || agent.name,
            version: agent.version || "unversioned",
            card_url: cardUrl,
            skills,
          },
          skills,
          scenarios,
          spec: `Deeply validate every advertised executable skill, not merely its Agent Card declaration. For each skill generate separate atomic tests for positive invocation, protocol/schema, malformed input, and latency. Add semantic accuracy tests only for declared expected responses. Exercise MCP tools/list and safe declared tool calls when MCP is advertised. Verify applicable authentication, idempotency, cancellation, timeout, and orchestration behavior only when declared by the contract or source evidence. Never invent an endpoint, payload, or expected answer. Executable scenarios: ${JSON.stringify(executableScenarios)}. Contract gaps that must be reported as requirements_needed rather than passed: ${JSON.stringify(contractGaps)}.`,
          repair: true,
          max_latency_ms: Number(probe.max_latency_ms),
          min_accuracy: Number(probe.min_accuracy),
        };
        if (repositoryAgent?.source_ref) {
          run.source_repository = repositoryAgent.repository;
          run.source_ref = repositoryAgent.source_ref;
          run.source_paths = [repositoryAgent.path.replace(/\/agent\.yaml$/, "")];
        }
        return [run];
      });
      if (!runs.length) throw new Error("no agents publish executable scenarios; complete the reported execution contracts first");
      const endpointRequired = repositoryDiscovery.agents.filter(
        (candidate) => !matchedRepositoryAgents.has(`${candidate.repository}:${candidate.path}`) && !candidate.card_url
      );
      const batch = await request(`${API.workflows}/batch`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ runs, max_concurrency: 1, replace_active: replaceActiveCampaign }),
      });
      const campaignContext = {
        runtime_agents: discovery.summary.discovered,
        executable_scenarios: discovery.summary.executable_scenarios,
        missing_execution_contracts: discovery.summary.missing_execution_contracts,
        missing_semantic_oracles: discovery.summary.missing_oracles,
        repository_agents: repositoryDiscovery.agents.length,
        source_grounded: matchedRepositoryAgents.size,
        requirements_needed: requirementsNeededAgents,
        endpoint_required: endpointRequired.map((candidate) => ({ repository: candidate.repository, path: candidate.path, name: candidate.name })),
        repository_errors: repositoryDiscovery.errors || [],
      };
      setAutopilotResult({ ...batch, status: "RUNNING", ...campaignContext });
      log(`autopilot accepted: ${batch.workflow_id} / ${runs.length} runtime agents / ${repositoryDiscovery.agents.length} repository manifests`);
      for (let attempt = 0; attempt < 2400; attempt += 1) {
        await new Promise((resolve) => setTimeout(resolve, 3000));
        const current = await request(`${API.workflows}/${batch.workflow_id}`);
        setAutopilotResult({ ...current, ...campaignContext });
        if (["COMPLETED", "FAILED", "CANCELED", "TERMINATED", "TIMED_OUT"].includes(current.status)) {
          log(`autopilot ${current.status.toLowerCase()}: ${batch.workflow_id}`);
          break;
        }
      }
    } catch (error) {
      setAutopilotResult({ status: "FAILED", error: error.message });
      log(`autopilot failed: ${error.message}`);
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
      <nav className="view-tabs" aria-label="Console views">
        <button className={activeView === "discover" ? "active" : ""} onClick={() => setActiveView("discover")}>01 / DISCOVER</button>
        <button className={activeView === "requirements" ? "active" : ""} onClick={() => setActiveView("requirements")}>02 / REQUIREMENTS</button>
        <button className={activeView === "execute" ? "active" : ""} onClick={() => setActiveView("execute")}>03 / EXECUTE</button>
        <button className={activeView === "observe" ? "active" : ""} onClick={() => setActiveView("observe")}>04 / OBSERVE</button>
      </nav>
      <ModelProviderStatus provider={diagnostics.model_provider} />
      <GithubStatus github={github} />
      <KnowledgeStatus stats={knowledgeStats} graph={graph} />

      {activeView === "discover" && (
        <Panel index="01" title="AUTOPILOT / FLEET QUALITY CAMPAIGN">
          <p className="workflow-intro">Discover runtime agents and repository manifests, correlate immutable source versions, design deep skill tests, execute them through Temporal, diagnose failures, and catalog only quality-gated evidence.</p>
          <div className="autopilot-callout">
            <div><small>RUNTIME + CONNECTED GITHUB REPOSITORIES</small><strong>AUTOPILOT MODE</strong><span>discover → correlate source → design → generate → execute → diagnose → catalog</span></div>
            <button type="button" onClick={runAutopilot} disabled={Boolean(busy)}>{busy === "autopilot" ? "AUTOPILOT RUNNING..." : "RUN AUTOPILOT ON ENTIRE FLEET →"}</button>
          </div>
          <label className="toggle"><input type="checkbox" checked={replaceActiveCampaign} onChange={(event) => setReplaceActiveCampaign(event.target.checked)} /> REPLACE ACTIVE CAMPAIGN (CANCEL RUNNING FLEETS FIRST)</label>
          {autopilotResult && <pre className="result autopilot-result">{JSON.stringify(autopilotResult, null, 2)}</pre>}
          <CatalogLinks workflow={autopilotResult} />
        </Panel>
      )}

      {activeView === "requirements" && (
        <Panel index="02" title="INGEST + REFINE REQUIREMENTS">
          <p className="workflow-intro">Upload product evidence before generation. AQE extracts independently testable requirements, stores their provenance, and carries them into the selected agent or website run.</p>
          <label>REQUIREMENTS DOCUMENT <em>txt, md, rst, json, yaml, csv, html, pdf, docx</em></label>
          <label className="file-drop"><input type="file" accept=".txt,.md,.rst,.json,.yaml,.yml,.csv,.html,.htm,.pdf,.docx" onChange={uploadDocument} disabled={busy === "document"} /><span>{busy === "document" ? "INGESTING REQUIREMENTS..." : "+ SELECT REQUIREMENTS FILE"}</span></label>
          {documentResult && <pre className="result requirements-result">{documentResult.error ? documentResult.error : JSON.stringify(documentResult, null, 2)}</pre>}
        </Panel>
      )}

      {activeView === "discover" && (
        <Panel index="01B" title="DISCOVER + DESIGN AGENT TESTS">
          <p className="workflow-intro">Inspect one Agent Card or the configured fleet. Executable scenarios require a real invocation and prompt; semantic scoring additionally requires a declared oracle.</p>
          <form onSubmit={testAgent}>
            <label>AGENT CARD URL <em>optional; blank discovers configured fleet</em></label>
            <input type="url" placeholder="https://agent.example/.well-known/agent.json" value={probe.card_url} onChange={(event) => setProbe({ ...probe, card_url: event.target.value })} />
            <div className="inline-fields">
              <div><label>MAX LATENCY MS</label><input type="number" min="1" value={probe.max_latency_ms} onChange={(event) => setProbe({ ...probe, max_latency_ms: event.target.value })} /></div>
              <div><label>MIN ACCURACY</label><input type="number" min="0" max="1" step="0.01" value={probe.min_accuracy} onChange={(event) => setProbe({ ...probe, min_accuracy: event.target.value })} /></div>
            </div>
            <div className="example-row">
              <button type="button" className="ghost" onClick={(event) => testAgent(event, true)} disabled={busy === "probe"}>{busy === "probe" ? "DISCOVERING..." : "DISCOVER ALL CONFIGURED AGENTS"}</button>
              <button disabled={busy === "probe"}>{busy === "probe" ? "DISCOVERING..." : "DISCOVER CARD →"}</button>
            </div>
          </form>
          {probeResult && <pre className="result design-result">{JSON.stringify(probeResult, null, 2)}</pre>}
        </Panel>
      )}

      {activeView === "execute" && (
        <Panel index="03" title="GENERATE + EXECUTE ONE SUITE">
          <p className="workflow-intro">Run one source-grounded agent or website test suite. Requirements uploaded in the previous step remain attached to this run.</p>
          <form onSubmit={startRun}>
            <label>TARGET URL</label>
            <input type="url" required placeholder="https://system-under-test.example" value={run.url} onChange={(event) => setRun({ ...run, url: event.target.value })} />
            <label>TEST RUNTIME</label>
            <select value={run.test_type} onChange={(event) => setRun({ ...run, test_type: event.target.value })}>
              <option value="agent">AGENT / HTTP + AGENT CARD</option>
              <option value="website">WEBSITE / PLAYWRIGHT BROWSER</option>
            </select>
            {run.test_type === "agent" && <><label>AGENT CARD URL</label><input type="url" required placeholder="https://agent.example/.well-known/agent.json" value={run.agent_card_url} onChange={(event) => setRun({ ...run, agent_card_url: event.target.value })} /></>}
            <div className="example-row">
              <button type="button" className="ghost" onClick={() => setRun(RUN_EXAMPLES.agent)}>LOAD AGENT EXAMPLE</button>
              <button type="button" className="ghost" onClick={() => setRun(RUN_EXAMPLES.website)}>LOAD WEBSITE EXAMPLE</button>
            </div>
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
          <CatalogLinks workflow={runResult} />
        </Panel>
      )}

      {activeView === "execute" && (
        <div className="grid secondary-grid">
        <Panel index="03B" title="AGENT FLEET" action={<button className="ghost" onClick={() => scan(true)} disabled={busy === "scan"}>SAFE HEAL</button>}>
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
        <Panel index="LOG" title="EVENT STREAM">
          <ol className="events">
            {events.map((event, index) => <li key={`${event}-${index}`}><time>{String(index).padStart(2, "0")}</time><span>{event}</span></li>)}
          </ol>
        </Panel>
        </div>
      )}

      {activeView === "observe" && <div className="example-row observe-switch">
        <button type="button" className={observeMode === "graph" ? "active" : "ghost"} onClick={() => setObserveMode("graph")}>LIVE GRAPH</button>
        <button type="button" className={observeMode === "topology" ? "active" : "ghost"} onClick={() => setObserveMode("topology")}>3D TOPOLOGY</button>
      </div>}

      {activeView === "observe" && observeMode === "graph" && (
        <Panel index="04" title="LIVE AGENT + TEST GRAPH" action={<span className="graph-stats">{graph.stats.node_count}N / {graph.stats.edge_count}E // POLL 5S</span>}>
          <TopologyGraph graph={graph} selected={selectedNode} onSelect={setSelectedNode} github={github} />
          <WorkflowHistory history={workflowHistory} />
        </Panel>
      )}

      {activeView === "observe" && observeMode === "topology" && (
        <Panel index="04" title="STABLE AGENT TOPOLOGY" action={<span className="graph-stats">LIVE // WORKFLOW HISTORY IN 2D</span>}>
          <Topology3D graph={graph} />
        </Panel>
      )}

      <footer><span>AQE/2.0</span><span>Temporal durable state</span><span>RustFS evidence store</span><span>one observable outcome / test</span></footer>
    </main>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
