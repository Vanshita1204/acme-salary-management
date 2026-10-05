import { NavLink, Route, Routes } from "react-router-dom";
import { Alert, Loading } from "./components/ui";
import { useActor, useReference, useReporting } from "./lib/context";
import AnalyticsPage from "./pages/AnalyticsPage";
import DirectoryPage from "./pages/DirectoryPage";
import EmployeePage from "./pages/EmployeePage";
import ImportPage from "./pages/ImportPage";
import NewEmployeePage from "./pages/NewEmployeePage";
import ReferencePage from "./pages/ReferencePage";

function Shell() {
  const { actor, setActor } = useActor();
  const { currency, setCurrency, available } = useReporting();
  return (
    <>
      <a className="skip" href="#main">Skip to content</a>
      <header className="topbar">
        <div className="topbar-inner">
          <NavLink to="/" className="brand">ACME Salary Management</NavLink>
          <nav className="nav" aria-label="Main">
            <NavLink to="/" end>Employees</NavLink>
            <NavLink to="/analytics">Pay insights</NavLink>
            <NavLink to="/import">Import</NavLink>
            <NavLink to="/reference">Reference data</NavLink>
          </nav>
          <div className="topbar-tools">
            <label>
              Reporting currency
              <select value={currency} onChange={(event) => setCurrency(event.target.value)} aria-label="Reporting currency">
                {available.map((code) => (
                  <option key={code} value={code}>{code}</option>
                ))}
              </select>
            </label>
            <label>
              Acting as
              <input
                value={actor}
                onChange={(event) => setActor(event.target.value)}
                placeholder="your name or email"
                aria-label="Acting as (recorded on changes)"
              />
            </label>
          </div>
        </div>
      </header>
      <main id="main">
        <Content />
      </main>
    </>
  );
}

/** Screens need the reference lists (filters, dropdowns); wait for them once, here. */
function Content() {
  const { ref, loading, error, refresh } = useReference();
  if (!ref) {
    if (error) {
      return (
        <Alert title="Can't load the app's reference data">
          {error}{" "}
          <button type="button" className="link" onClick={refresh}>Try again</button>
        </Alert>
      );
    }
    return loading ? <Loading /> : null;
  }
  return (
    <Routes>
      <Route path="/" element={<DirectoryPage />} />
      <Route path="/employees/new" element={<NewEmployeePage />} />
      <Route path="/employees/:id" element={<EmployeePage />} />
      <Route path="/analytics" element={<AnalyticsPage />} />
      <Route path="/import" element={<ImportPage />} />
      <Route path="/reference" element={<ReferencePage />} />
      <Route path="*" element={<Alert kind="info" title="Page not found">Use the menu above to find what you need.</Alert>} />
    </Routes>
  );
}

export default function App() {
  return <Shell />;
}
