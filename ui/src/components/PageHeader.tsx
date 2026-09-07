/**
 * The header every page starts with.
 *
 * The description is not decoration. "Settings" was the page people opened
 * looking for configuration and found a YAML textarea; a page that says what it
 * is in one line costs nothing and answers that before the click does.
 *
 * Actions sit in a `no-grow` row for the reason spelled out in styles.css: a
 * button next to a flexible title must be told, explicitly, not to shrink.
 */

import type { ReactNode } from "react";

export function PageHeader({
  title,
  description,
  children,
}: {
  title: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <div className="page-header">
      <div className="page-header-text">
        <h1 className="page-title">{title}</h1>
        {description && <p className="page-description">{description}</p>}
      </div>
      {children && <div className="page-header-actions no-grow">{children}</div>}
    </div>
  );
}
