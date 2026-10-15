import { useTimings } from "../api/timing";

const ms = (value: number | null) => (value === null ? "—" : `${Math.round(value)} ms`);

/** Server time versus browser round trip for the views on a page, so a slow page can be
 *  traced to the server or to the network and hosting. */
export function TimingNote({ prefix }: { prefix: string }) {
  const timings = useTimings(prefix);
  if (timings.length === 0) return null;
  const servers = timings.map((t) => t.serverMs).filter((v): v is number => v !== null);
  const slowestServer = servers.length ? Math.max(...servers) : null;
  const slowestTotal = Math.max(...timings.map((t) => t.totalMs));
  return (
    <details className="muted timing-note">
      <summary>
        Server time: slowest view {ms(slowestServer)} (request received to response sent). Browser round trip: {ms(slowestTotal)}.
      </summary>
      <div className="table-wrap" tabIndex={0}>
        <table>
          <thead>
            <tr><th scope="col">View</th><th scope="col" className="num">Server</th><th scope="col" className="num">Round trip</th><th scope="col" className="num">Network and wait</th></tr>
          </thead>
          <tbody>
            {timings.map((t) => (
              <tr key={t.path}>
                <td>{t.path.replace(prefix, "") || t.path}</td>
                <td className="num">{ms(t.serverMs)}{t.cached && " (cached)"}</td>
                <td className="num">{ms(t.totalMs)}</td>
                <td className="num">{t.serverMs === null ? "—" : ms(Math.max(0, t.totalMs - t.serverMs))}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p>Round trip includes the network and, on a free hosted tier, waiting for an idle service to wake up. Server time does not. "Cached" means the server reused a recent identical answer instead of recomputing it.</p>
    </details>
  );
}
