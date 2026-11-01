/**
 * Helper functions for deterministic chart data sampling and normalization.
 */

/**
 * Deterministically samples array using uniform stride, ensuring endpoints are preserved.
 */
export function sampleUniformStride<T>(items: T[], maxPoints: number): T[] {
  if (items.length <= maxPoints || maxPoints <= 1) {
    return items
  }
  const result: T[] = []
  const step = (items.length - 1) / (maxPoints - 1)
  for (let i = 0; i < maxPoints; i++) {
    const idx = Math.min(items.length - 1, Math.round(i * step))
    result.push(items[idx])
  }
  return result
}

