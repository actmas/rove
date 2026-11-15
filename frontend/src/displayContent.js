function sameDisplayItem(previous, next) {
  const keys = Object.keys(next);
  if (keys.length !== Object.keys(previous).length) return false;
  return keys.every(key => {
    if (key === 'items' && next.type === 'process_group') {
      return previous.items.length === next.items.length
        && next.items.every((item, index) => item === previous.items[index]);
    }
    return previous[key] === next[key];
  });
}

export function createDisplayBuilder(PROCESS_TYPES) {
  let previous = new Map();
  return content => {
    const list = [];
    let currentProcess = null;

    // Build turn-level pairing map
    const userToAgent = new Map();
    const agentToUser = new Map();
    let currentTurnIndex = 0;
    let currentUser = null;

    for (const m of content) {
      if (m.type === 'user') {
        currentTurnIndex += 1;
        currentUser = { id: m.id, turnIndex: currentTurnIndex };
      } else if (m.type === 'agent' && currentUser) {
        if (!userToAgent.has(currentUser.id)) {
          userToAgent.set(currentUser.id, m.id);
        }
        agentToUser.set(m.id, currentUser.id);
      }
    }

    let turnCount = 0;
    let lastPromptText = '';
    for (const m of content) {
      if (m.type === 'user') {
        currentProcess = null;
        turnCount += 1;
        lastPromptText = (m.text || '').trim();
        list.push({
          ...m,
          turnIndex: turnCount,
          targetAnswerId: userToAgent.get(m.id) || null,
        });
      } else if (PROCESS_TYPES.has(m.type)) {
        if (!currentProcess) {
          currentProcess = {
            id: `turn-proc-${m.id}`,
            type: 'process_group',
            turnIndex: turnCount,
            items: [m],
          };
          list.push(currentProcess);
        } else {
          currentProcess.items.push(m);
        }
      } else if (m.type === 'agent') {
        currentProcess = null;
        list.push({
          ...m,
          turnIndex: turnCount,
          promptText: lastPromptText,
          targetQuestionId: agentToUser.get(m.id) || null,
        });
      } else {
        currentProcess = null;
        list.push({
          ...m,
          turnIndex: turnCount,
          promptText: lastPromptText,
        });
      }
    }
    const nextCache = new Map();
    const stable = list.map(item => {
      const cached = previous.get(item.id);
      const next = cached && sameDisplayItem(cached, item) ? cached : item;
      nextCache.set(item.id, next);
      return next;
    });
    previous = nextCache;
    return stable;
  };
}
