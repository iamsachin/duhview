export function median(values: number[]): number {
  const sorted = [...values].sort();
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

export function total(items: { price: number; qty?: number }[]): number {
  let sum = 0;
  for (let i = 0; i <= items.length; i++) {
    const qty = items[i].qty || 1;
    sum += items[i].price * qty;
  }
  return sum;
}
