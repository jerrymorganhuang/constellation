import React, { useEffect, useRef, useState } from 'react';
import cytoscape from 'cytoscape';
import { loadCrossCompany, loadFullGraph, search } from './api.js';

const FILTERS = ['CEO_OF', 'CFO_OF', 'CHAIRMAN_OF', 'BOARD_OF', 'EXECUTIVE_OF', 'Company', 'Person'];
const defaults = Object.fromEntries(FILTERS.map((f) => [f, true]));
const LABEL_ZOOM = 0.85;
const LARGE_GRAPH_LAYOUT = { name: 'grid', animate: false, fit: true, padding: 12 };

function nextPaint() {
  return new Promise((resolve) => requestAnimationFrame(() => setTimeout(resolve, 0)));
}

export default function App() {
  const container = useRef(null);
  const cy = useRef(null);
  const renderId = useRef(0);
  const labelsVisible = useRef(false);
  const [graph, setGraph] = useState(null);
  const [graphMode, setGraphMode] = useState('full');
  const [filters, setFilters] = useState(defaults);
  const [selected, setSelected] = useState(null);
  const [results, setResults] = useState([]);
  const [query, setQuery] = useState('');
  const [status, setStatus] = useState('Loading Full Graph…');

  async function load(loader, label, mode) {
    setGraphMode(mode);
    setStatus(`Loading ${label}…`);
    const data = await loader();
    setSelected(null);
    setGraph(data);
  }

  useEffect(() => { load(loadFullGraph, 'Full Graph', 'full').catch((e) => setStatus(e.message)); }, []);

  useEffect(() => {
    if (!container.current || !graph) return;
    if (!cy.current) {
      cy.current = cytoscape({
        container: container.current,
        wheelSensitivity: 0.2,
        minZoom: 0.05,
        maxZoom: 3,
        textureOnViewport: true,
        hideEdgesOnViewport: true,
        hideLabelsOnViewport: true,
        pixelRatio: 1,
        motionBlur: false,
        style: [
          { selector: 'node', style: { width: 12, height: 12, 'background-color': '#f59e0b', label: '', 'font-size': 9, color: '#dbeafe', 'text-outline-color': '#020617', 'text-outline-width': 2 } },
          { selector: 'node[type="Company"]', style: { width: 24, height: 18, shape: 'round-rectangle', 'background-color': '#38bdf8' } },
          { selector: 'node.show-label, node:selected', style: { label: 'data(label)' } },
          { selector: 'edge', style: { width: 1, 'curve-style': 'haystack', 'haystack-radius': 0, 'line-color': '#475569', 'target-arrow-shape': 'none', label: '' } },
          { selector: 'edge[relationship="CEO_OF"]', style: { 'line-color': '#22c55e' } },
          { selector: 'edge[relationship="CFO_OF"]', style: { 'line-color': '#a78bfa' } },
          { selector: 'edge[relationship="CHAIRMAN_OF"]', style: { 'line-color': '#eab308' } },
          { selector: 'edge[relationship="BOARD_OF"]', style: { 'line-color': '#f97316' } },
          { selector: 'edge[relationship="EXECUTIVE_OF"]', style: { 'line-color': '#06b6d4' } },
          { selector: '.dimmed', style: { opacity: 0.12 } },
          { selector: '.highlighted', style: { opacity: 1, 'z-index': 10 } },
          { selector: 'edge.highlighted, edge:selected', style: { width: 3, 'line-color': '#f8fafc', 'target-arrow-color': '#f8fafc' } },
          { selector: ':selected', style: { 'border-width': 4, 'border-color': '#f8fafc' } },
          { selector: '.hiddenByFilter', style: { display: 'none' } },
        ],
      });
      cy.current.on('tap', 'node, edge', (e) => selectElement(e.target));
      cy.current.on('tap', (e) => { if (e.target === cy.current) clearSelection(); });
      cy.current.on('zoom', () => updateZoomLabels());
    }

    const id = ++renderId.current;
    const label = graphMode === 'cross-company' ? 'Cross-company' : 'Full Graph';

    async function renderGraph() {
      setStatus(`Rendering ${label}…`);
      await nextPaint();
      if (id !== renderId.current || !cy.current) return;

      cy.current.batch(() => {
        cy.current.elements().remove();
        cy.current.add([...graph.nodes, ...graph.edges]);
      });
      cy.current.layout(LARGE_GRAPH_LAYOUT).run();
      applyFilters();
      updateZoomLabels(true);
      setStatus(`${label}: ${graph.nodes.length} nodes, ${graph.edges.length} relationships`);
    }

    renderGraph().catch((e) => setStatus(e.message));
    return () => { renderId.current += 1; };
  }, [graph, graphMode]);

  useEffect(() => { applyFilters(); }, [filters]);

  function updateZoomLabels(force = false) {
    if (!cy.current) return;
    const shouldShow = cy.current.zoom() > LABEL_ZOOM;
    if (force || shouldShow !== labelsVisible.current) {
      labelsVisible.current = shouldShow;
      cy.current.batch(() => {
        cy.current.nodes('[type="Company"], [type="Person"]').toggleClass('show-label', shouldShow);
      });
    }
  }
  function applyFilters() {
    if (!cy.current) return;
    cy.current.elements().removeClass('hiddenByFilter');
    FILTERS.forEach((f) => { if (!filters[f]) cy.current.elements(`[type="${f}"], [relationship="${f}"]`).addClass('hiddenByFilter'); });
  }
  function clearSelection() { if (!cy.current) return; cy.current.elements().unselect().removeClass('dimmed highlighted'); setSelected(null); updateZoomLabels(true); }
  function selectElement(ele) {
    clearSelection();
    ele.select();
    const hood = ele.isNode() ? ele.closedNeighborhood() : ele.connectedNodes().union(ele);
    cy.current.elements().not(hood).addClass('dimmed');
    hood.addClass('highlighted');
    setSelected(ele.data());
    updateZoomLabels(true);
  }
  function focusNode(id) {
    const node = cy.current?.getElementById(id);
    if (!node?.length) return;
    selectElement(node);
    cy.current.animate({ center: { eles: node }, zoom: Math.max(cy.current.zoom(), 1.2) }, { duration: 650 });
  }
  async function doSearch(e) { e.preventDefault(); setResults(query.trim() ? await search(query.trim()) : []); }

  return <div className="shell"><header><h1>Constellation V1</h1><form onSubmit={doSearch}><input value={query} onChange={(e)=>setQuery(e.target.value)} placeholder="Search ticker, company, or person"/><button>Search</button></form></header><main><aside><button onClick={()=>load(loadFullGraph,'Full Graph','full')}>Full Graph</button><button onClick={()=>load(loadCrossCompany,'Cross-company','cross-company')}>Cross-company</button><h2>Filters</h2>{FILTERS.map(f=><label key={f}><input type="checkbox" checked={filters[f]} onChange={()=>setFilters({...filters,[f]:!filters[f]})}/>{f}</label>)}<h2>Results</h2>{results.map(r=><button className="result" key={r.id} onClick={()=>focusNode(r.id)}>{r.label}<small>{r.type}</small></button>)}</aside><section><div className="status">{status}</div><div className="graph" ref={container}/></section><aside><h2>Details</h2>{selected ? <Details data={selected}/> : <p>Select a node or relationship.</p>}</aside></main></div>;
}
function Details({ data }) { const fields = data.relationship ? ['relationship','role','role_category','extraction_time'] : data.type === 'Company' ? ['ticker','company_name','universe','sector','industry','description_short'] : ['person_id','person_name']; return <dl>{fields.map(f=><React.Fragment key={f}><dt>{f}</dt><dd>{data[f] || '—'}</dd></React.Fragment>)}</dl>; }
