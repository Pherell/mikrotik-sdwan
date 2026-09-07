/**
 * Keyboard navigation.
 *
 * `g` then a letter, in the manner of every other tool an operator already has
 * open. `?` lists them, Escape backs out of whatever is covering the page.
 *
 * Everything here is inert while focus is in a field. A controller where typing
 * "gs" into a site name navigates away mid-form would be worse than having no
 * shortcuts at all.
 */

import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

const DESTINATIONS: Record<string, { to: string; label: string }> = {
  o: { to: "/", label: "Overview" },
  s: { to: "/sites", label: "Sites" },
  f: { to: "/fabrics", label: "Fabrics" },
  p: { to: "/policies", label: "Policies" },
  j: { to: "/jobs", label: "Jobs" },
};

function isTyping(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  if (!el) return false;
  const tag = el.tagName;
  return (
    tag === "INPUT" ||
    tag === "TEXTAREA" ||
    tag === "SELECT" ||
    el.isContentEditable
  );
}

export function Shortcuts({ onEscape }: { onEscape: () => void }) {
  const navigate = useNavigate();
  const [showing, setShowing] = useState(false);
  // "g" arms the next keypress. It expires, so a stray g does not lie in wait.
  const armed = useRef<number | null>(null);

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setShowing(false);
        onEscape();
        return;
      }
      if (isTyping(event.target) || event.ctrlKey || event.metaKey || event.altKey) {
        return;
      }

      if (event.key === "?") {
        event.preventDefault();
        setShowing((s) => !s);
        return;
      }

      if (armed.current !== null) {
        window.clearTimeout(armed.current);
        armed.current = null;
        const destination = DESTINATIONS[event.key.toLowerCase()];
        if (destination) {
          event.preventDefault();
          navigate(destination.to);
        }
        return;
      }

      if (event.key === "g") {
        armed.current = window.setTimeout(() => {
          armed.current = null;
        }, 1500);
      }
    }

    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      if (armed.current !== null) window.clearTimeout(armed.current);
    };
  }, [navigate, onEscape]);

  if (!showing) return null;

  return (
    <div
      className="shortcuts-backdrop"
      onClick={() => setShowing(false)}
      role="dialog"
      aria-modal="true"
      aria-label="Keyboard shortcuts"
    >
      <div className="shortcuts card" onClick={(e) => e.stopPropagation()}>
        <h2>Keyboard shortcuts</h2>
        <dl>
          {Object.entries(DESTINATIONS).map(([key, d]) => (
            <div key={key} style={{ display: "contents" }}>
              <dt>
                <kbd>g</kbd> <kbd>{key}</kbd>
              </dt>
              <dd>{d.label}</dd>
            </div>
          ))}
          <dt>
            <kbd>?</kbd>
          </dt>
          <dd>This list</dd>
          <dt>
            <kbd>Esc</kbd>
          </dt>
          <dd>Close whatever is covering the page</dd>
        </dl>
      </div>
    </div>
  );
}
