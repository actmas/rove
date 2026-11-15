let database;
function open() {
  if (!database) {
    const pending = new Promise((resolve, reject) => {
      const request = indexedDB.open('rove-history', 1);
      request.onupgradeneeded = () => request.result.createObjectStore('sessions');
      request.onsuccess = () => {
        const db = request.result;
        const invalidate = () => {
          if (database === pending) database = null;
        };
        db.onversionchange = () => { db.close(); invalidate(); };
        db.onclose = invalidate;
        resolve(db);
      };
      request.onerror = () => reject(request.error);
    });
    database = pending;
    pending.catch(() => {
      if (database === pending) database = null;
    });
  }
  return database;
}
export async function readHistory(id) {
  try {
    const db = await open();
    return await new Promise((resolve, reject) => {
      const request = db.transaction('sessions').objectStore('sessions').get(id);
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
  } catch { return null; }
}
export async function writeHistory(id, value) {
  try {
    const db = await open();
    await new Promise((resolve, reject) => {
      const transaction = db.transaction('sessions', 'readwrite');
      transaction.oncomplete = resolve;
      transaction.onerror = () => reject(transaction.error);
      transaction.onabort = () => reject(transaction.error || new Error('History cache transaction aborted'));
      const store = transaction.objectStore('sessions');
      store.put(value, id);
      try {
        const keyReq = store.getAllKeys();
        keyReq.onsuccess = () => {
          const keys = keyReq.result;
          if (Array.isArray(keys) && keys.length > 50) {
            const excess = keys.slice(0, keys.length - 50);
            for (const k of excess) {
              store.delete(k);
            }
          }
        };
      } catch {}
    });
  } catch { /* Cache failure leaves network recovery available. */ }
}

export async function deleteHistory(id) {
  try {
    const db = await open();
    await new Promise((resolve, reject) => {
      const transaction = db.transaction('sessions', 'readwrite');
      transaction.oncomplete = resolve;
      transaction.onerror = () => reject(transaction.error);
      transaction.objectStore('sessions').delete(id);
    });
  } catch {}
}
