import {useMemo} from 'react';

const ALLOWED_TONES = new Set(['primary', 'secondary', 'tertiary', 'success', 'warning', 'danger']);

function normalizeTone(raw) {
  const val = String(raw || 'primary').trim().toLowerCase();
  return ALLOWED_TONES.has(val) ? val : 'primary';
}

export function parseRoveUiLines(source) {
  const nodes = {};
  const roots = [];
  for (const rawLine of String(source || '').split('\n')) {
    const line = rawLine.trim();
    if (!line) continue;
    let opObj;
    try {
      opObj = JSON.parse(line);
    } catch {
      continue;
    }
    if (!opObj || typeof opObj !== 'object') continue;
    const {op, id, parent, type, ...rest} = opObj;
    if (!id || typeof id !== 'string') continue;

    if (op === 'remove') {
      delete nodes[id];
      const rIdx = roots.indexOf(id);
      if (rIdx >= 0) roots.splice(rIdx, 1);
      for (const n of Object.values(nodes)) {
        n.children = n.children.filter(c => c !== id);
      }
      continue;
    }

    const sanitized = {};
    for (const [k, v] of Object.entries(rest)) {
      if (k === 'backgroundColor' || k === 'borderColor' || k === 'color' || k === 'style' || k === 'className') {
        continue;
      }
      sanitized[k] = k === 'tone' ? normalizeTone(v) : v;
    }

    if (op === 'patch') {
      if (nodes[id]) {
        nodes[id].props = {...nodes[id].props, ...sanitized};
      }
      continue;
    }

    if (op === 'create' || op === 'append') {
      nodes[id] = {id, type: String(type || 'text'), props: sanitized, children: []};
      if (op === 'create') {
        if (!roots.includes(id)) roots.push(id);
      } else if (parent && nodes[parent]) {
        if (!nodes[parent].children.includes(id)) nodes[parent].children.push(id);
      } else if (!roots.includes(id)) {
        roots.push(id);
      }
    }
  }
  return {roots, nodes};
}

function renderNode(nodeId, nodes, onSend) {
  const node = nodes[nodeId];
  if (!node) return null;
  const {type, props, children} = node;
  const tone = normalizeTone(props.tone);

  if (type === 'card') {
    return (
      <div className="rove-ui-card" key={nodeId}>
        {props.title && <div className="rove-ui-card-title">{String(props.title)}</div>}
        <div className="rove-ui-card-body">
          {children.map(cid => renderNode(cid, nodes, onSend))}
        </div>
      </div>
    );
  }
  if (type === 'row') {
    return (
      <div className="rove-ui-row" key={nodeId}>
        {children.map(cid => renderNode(cid, nodes, onSend))}
      </div>
    );
  }
  if (type === 'stat') {
    return (
      <div className={`rove-ui-stat rove-ui-tone-${tone}`} key={nodeId}>
        {props.label && <span className="rove-ui-stat-label">{String(props.label)}</span>}
        <span className="rove-ui-stat-value">{String(props.value ?? '')}</span>
      </div>
    );
  }
  if (type === 'badge') {
    return (
      <span className={`rove-ui-badge rove-ui-tone-${tone}`} key={nodeId}>
        {String(props.label ?? props.value ?? '')}
      </span>
    );
  }
  if (type === 'bar') {
    const pct = Math.max(0, Math.min(100, Number(props.percent) || 0));
    return (
      <div className="rove-ui-bar" key={nodeId}>
        <div className="rove-ui-bar-head">
          <span>{String(props.label || '')}</span>
          <span>{pct}%</span>
        </div>
        <div className="rove-ui-bar-track">
          <div className={`rove-ui-bar-fill rove-ui-tone-${tone}`} style={{width: `${pct}%`}}/>
        </div>
      </div>
    );
  }
  if (type === 'button') {
    const sendText = typeof props.send === 'string' ? props.send : '';
    return (
      <button
        type="button"
        className={`rove-ui-btn rove-ui-tone-${tone}`}
        key={nodeId}
        disabled={!sendText || !onSend}
        onClick={() => sendText && onSend && onSend(sendText)}
      >
        {String(props.label || '执行')}
      </button>
    );
  }
  if (type === 'divider') {
    return <hr className="rove-ui-divider" key={nodeId}/>;
  }
  return (
    <p className={`rove-ui-text rove-ui-tone-${tone}`} key={nodeId}>
      {String(props.text ?? props.value ?? '')}
    </p>
  );
}

export default function RichUiBlock({source, onSend}) {
  const tree = useMemo(() => parseRoveUiLines(source), [source]);
  if (!tree.roots.length) {
    return <pre className="rove-ui-fallback"><code>{source}</code></pre>;
  }
  return (
    <div className="rove-ui-root">
      {tree.roots.map(rid => renderNode(rid, tree.nodes, onSend))}
    </div>
  );
}
