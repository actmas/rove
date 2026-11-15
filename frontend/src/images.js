// Only image sources receive the extra local-file and inline-image URL handling.
export function imageSource(src, base, sessionId) {
  if (typeof src !== 'string' || !src.trim()) return '';
  const value = src.trim();
  if (/^data:image\/(png|jpeg|webp|gif|avif);base64,[a-z0-9+/=\s]+$/i.test(value)) return value;
  if (/^https?:\/\//i.test(value)) return value;
  if (/^\/\//.test(value)) return '';
  if (/^[a-z][a-z0-9+.-]*:/i.test(value) && !/^(file:\/\/|sandbox:)/i.test(value)) return '';
  // Preserve application URLs instead of treating them as disk paths.
  if (/^\/(api\/|assets\/)/.test(value)) return new URL(value, base).toString();
  const path = value.replace(/^(file:\/\/|sandbox:)/i, '');
  const url = new URL('api/media', base);
  url.searchParams.set('path', path);
  if (sessionId) url.searchParams.set('session_id', sessionId);
  return url.toString();
}

export function itemImages(item) {
  const sources = [item.path, item.savedPath, item.imageUrl, item.image_url];
  const result = item.result;
  if (item.type === 'imageGeneration' && typeof result === 'string') {
    if (/^(data:image\/|https?:\/\/|\/|file:\/\/)/i.test(result)) sources.push(result);
    else if (result.length >= 4 && /^[a-z0-9+/=\s]+$/i.test(result)) sources.push(`data:image/png;base64,${result}`);
  }
  for (const block of result?.content || []) {
    if (block.type === 'image' && block.data && /^image\/(png|jpeg|webp|gif|avif)$/.test(block.mimeType)) {
      sources.push(`data:${block.mimeType};base64,${block.data}`);
    }
  }
  const inline = sources.find(value => typeof value === 'string' && value.startsWith('data:image/'));
  if (inline && item.type === 'imageGeneration') return [inline];
  return [...new Set(sources.filter(value => typeof value === 'string' && value))];
}
