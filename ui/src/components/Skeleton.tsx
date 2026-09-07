/**
 * A shape while the data loads, instead of the word "Loading…".
 *
 * The point is not decoration: it reserves the space the content will occupy,
 * so the page does not jump when the request lands.
 */

export function Skeleton({
  rows = 3,
  label = "Loading",
}: {
  rows?: number;
  label?: string;
}) {
  // Varying widths, deterministically -- a column of identical bars reads as a
  // rendering fault rather than as pending content.
  const widths = ["92%", "68%", "80%", "55%", "74%"];
  return (
    <div aria-busy="true" aria-label={label}>
      {Array.from({ length: rows }, (_, i) => (
        <div
          key={i}
          className="skeleton"
          style={{ width: widths[i % widths.length] }}
        />
      ))}
    </div>
  );
}
