import React, { useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import cytoscape from 'cytoscape';
import './styles.css';

const API_BASE = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000';
const DEFAULT_FILTERS = {
  CEO_OF: true,
  CFO_OF: true,
  BOARD_OF: true,
  Company: true,
  Person: true,
};

function App() {
  const cyRef = useRef(null);
  const containerRef = useRef(null);
  const [graph, setGraph] = useState({ nodes: [], edges: [] });
  const [activeView, setActiveView] = useState('Full Graph');
  const [filters, setFilters] = useState(DEFAULT_FILTERS);
  const [selected, setSelected] = useState(null);
  const [query, setQuery] = useState('');
  const [results, setResults] = useState([]);
  const [status, setStatus] = useState('Loading universal graph…');

  const visibleElements = useMemo(() => {
    const visibleNodeIds = new Set(
      graph.nodes
        .filter((node) => filters[node.data.type] !== false)
        .map((node) => node.data.id),
    );
    return [
      ...graph.nodes.filter((node) => visibleNodeIds.has(node.data.id)),
      ...graph.edges.filter(
        (edge) =>
          filters[edge.data.relationship] !== false &&
          visibleNodeIds.has(edge.data.source) &&
          visibleNodeIds.has(edge.data.target),
      ),
    ];
  }, [graph, filters]);

  async function loadGraph(path, label) {
    setStatus(`Loading ${label}…`);
    const response = await fetch(`${API_BASE}${path}`);
    if (!response.ok) throw new Error(await response.text());
    const data = await response.json();
    setGraph(data);
    setActiveView(label);
    setSelected(null);
    setStatus(`${label}: ${data.nodes.length} nodes, ${data.edges.length} edges`);
  }

  useEffect(() => {
    loadGraph('/graph/universal', 'Full Graph').catch((error) =>
      setStatus(`Unable to load graph: ${error.message}`),
    );
  }, []);

  useEffect(() => {
    if (!containerRef.current) return;
    if (!cyRef.current) {
      cyRef.current = cytoscape({
        container: containerRef.current,
        style: [
          { selector: 'node', style: { label: 'data(label)', 'font-size': 10, color: '#dbeafe', 'text-outline-color': '#0f172a', 'text-outline-width': 2 } },
          { selector: 'node[type="Company"]', style: { shape: 'round-rectangle', 'background-color': '#38bdf8', width: 42, height: 28 } },
          { selector: 'node[type="Person"]', style: { shape: 'ellipse', 'background-color': '#f59e0b', width: 26, height: 26 } },
          { selector: 'edge', style: { width: 1.5, 'curve-style': 'bezier', 'target-arrow-shape': 'triangle', 'line-color': '#64748b', 'target-arrow-color': '#64748b' } },
          { selector: 'edge[relationship="CEO_OF"]', style: { width: 3, 'line-color': '#22c55e', 'target-arrow-color': '#22c55e' } },
          { selector: 'edge[relationship="CFO_OF"]', style: { width: 2.5, 'line-color': '#a78bfa', 'target-arrow-color': '#a78bfa', 'line-style': 'dashed' } },
          { selector: 'edge[relationship="BOARD_OF"]', style: { 'line-color': '#f97316', 'target-arrow-color': '#f97316' } },
          { selector: 'edge[relationship="SHARES_PERSON"]', style: { width: 'mapData(shared_count, 1, 6, 2, 8)', 'line-color': '#e2e8f0', 'target-arrow-shape': 'none' } },
          { selector: ':selected', style: { 'border-width': 4, 'border-color': '#f8fafc', 'line-color': '#f8fafc', 'target-arrow-color': '#f8fafc' } },
        ],
      });
      cyRef.current.on('tap', 'node, edge', (event) => setSelected(event.target.data()));
      cyRef.current.on('tap', (event) => {
        if (event.target === cyRef.current) setSelected(null);
      });
    }
    const cy = cyRef.current;
    cy.elements().remove();
    cy.add(visibleElements);
    cy.layout({ name: 'cose', animate: false, idealEdgeLength: 90, nodeRepulsion: 8000 }).run();
    cy.fit(undefined, 40);
  }, [visibleElements]);

  async function runSearch(event) {
    event.preventDefault();
    if (!query.trim()) return setResults([]);
    const response = await fetch(`${API_BASE}/search?q=${encodeURIComponent(query.trim())}`);
    const data = await response.json();
    setResults(data.results || []);
  }

  function focusNode(id) {
    const cy = cyRef.current;
    const node = cy?.getElementById(id);
    if (node?.length) {
      cy.animate({ center: { eles: node }, zoom: 1.7 }, { duration: 350 });
      node.select();
      setSelected(node.data());
    }
  }

  async function localFocus(result) {
    if (result.type === 'Company') {
      await loadGraph(`/graph/company/${encodeURIComponent(result.ticker)}?radius=1`, 'Search Focus');
    } else {
      await loadGraph(`/graph/person/${encodeURIComponent(result.name)}?radius=1`, 'Search Focus');
    }
  }

  return (
    <main className="app-shell">
      <header className="topbar">
        <h1>Constellation</h1>
        <form onSubmit={runSearch} className="search-form">
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search ticker, company, or person" />
          <button type="submit">Search</button>
        </form>
      </header>
      <section className="workspace">
        <aside className="panel left-panel">
          <h2>Views</h2>
          <button className={activeView === 'Full Graph' ? 'active' : ''} onClick={() => loadGraph('/graph/universal', 'Full Graph')}>Full Graph</button>
          <button className={activeView === 'Cross-company' ? 'active' : ''} onClick={() => loadGraph('/graph/cross-company', 'Cross-company')}>Cross-company</button>
          <button className={activeView === 'Company Network' ? 'active' : ''} onClick={() => loadGraph('/graph/company-network', 'Company Network')}>Company Network</button>
          <button className={activeView === 'Search Focus' ? 'active' : ''} disabled>Search Focus</button>

          <h2>Filters</h2>
          {Object.keys(DEFAULT_FILTERS).map((key) => (
            <label key={key} className="toggle-row">
              <input type="checkbox" checked={filters[key]} onChange={() => setFilters((current) => ({ ...current, [key]: !current[key] }))} />
              {key}
            </label>
          ))}

          <h2>Search Results</h2>
          <div className="results">
            {results.map((result) => (
              <div className="result" key={result.id}>
                <button onClick={() => focusNode(result.id)}>{result.label}</button>
                <small>{result.type}{result.ticker ? ` · ${result.ticker}` : ''}</small>
                <button className="secondary" onClick={() => localFocus(result)}>Local focus</button>
              </div>
            ))}
          </div>
        </aside>
        <section className="graph-card">
          <div className="status-bar">{status}</div>
          <div className="graph" ref={containerRef} />
        </section>
        <aside className="panel details-panel">
          <h2>Details</h2>
          {selected ? <pre>{JSON.stringify(selected, null, 2)}</pre> : <p>Select a node or edge to inspect its details.</p>}
        </aside>
      </section>
    </main>
  );
}

createRoot(document.getElementById('root')).render(<App />);
