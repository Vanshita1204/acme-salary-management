import { useEffect, useRef, useState } from "react";

/**
 * Renders `children` (and so starts their requests) only once the placeholder has come near
 * the screen, and keeps them mounted after that. A page of heavy reports then asks for the
 * ones in view first instead of all at once. Without IntersectionObserver (old browsers,
 * tests) it renders immediately.
 */
export function LazyOnView({ title, children }: { title: string; children: React.ReactNode }) {
  const supported = typeof IntersectionObserver !== "undefined";
  const [seen, setSeen] = useState(!supported);
  const marker = useRef<HTMLElement>(null);

  useEffect(() => {
    if (seen || !marker.current) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setSeen(true);
          observer.disconnect();
        }
      },
      { rootMargin: "300px" },
    );
    observer.observe(marker.current);
    return () => observer.disconnect();
  }, [seen]);

  if (seen) return <>{children}</>;
  return (
    <section ref={marker} className="card" aria-label={title} style={{ minHeight: "9rem" }}>
      <h2>{title}</h2>
      <p className="muted">Loads when you scroll here.</p>
    </section>
  );
}
