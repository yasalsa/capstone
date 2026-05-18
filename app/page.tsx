import {
  getApprovedSenders,
  getRecentCases,
  getRecentQuarantine,
  getStats
} from "@/lib/repository";

import {
  addApprovedSenderAction,
  removeApprovedSenderAction
} from "@/app/actions";

export const dynamic = "force-dynamic";
export const revalidate = 0;

function formatDate(value: string): string {
  if (!value) {
    return "n/a";
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  return parsed.toLocaleString();
}

function truncate(text: string, maxLength: number): string {
  if (text.length <= maxLength) {
    return text;
  }
  return `${text.slice(0, Math.max(0, maxLength - 1))}\u2026`;
}

function summarizeScanResult(scanResultJson: string): string {
  try {
    const parsed = JSON.parse(scanResultJson) as {
      attachment_count?: number;
      risky_attachment_names?: string[];
      url_count?: number;
    };
    const attachments = parsed.attachment_count ?? 0;
    const risky = parsed.risky_attachment_names?.length ?? 0;
    const urls = parsed.url_count ?? 0;
    return `att:${attachments} risky:${risky} urls:${urls}`;
  } catch {
    return "scan:unavailable";
  }
}

function EmptyState({ text }: { text: string }) {
  return <p className="empty-state">{text}</p>;
}

export default async function DashboardPage() {
  let stats;
  let cases;
  let quarantine;
  let approvedSenderGroups;
  let setupError: string | null = null;

  try {
    [stats, cases, quarantine, approvedSenderGroups] = await Promise.all([
      getStats(),
      getRecentCases(75),
      getRecentQuarantine(75),
      getApprovedSenders()
    ]);
  } catch (error) {
    setupError =
      error instanceof Error
        ? error.message
        : "Failed to load dashboard data. Check DATABASE_URL and schema setup.";
    stats = {
      casesCount: 0,
      quarantineCount: 0,
      approvedSendersCount: 0,
      usersWithAllowlistCount: 0
    };
    cases = [];
    quarantine = [];
    approvedSenderGroups = [];
  }

  return (
    <main className="page-shell">
      <header className="hero">
        <p className="hero-eyebrow">Gmail Intake Ops</p>
        <h1>Threat Intake Dashboard</h1>
        <p className="hero-subtitle">
          Monitor processed cases, quarantine decisions, and sender allowlists for
          `scan+&lt;user_id&gt;@gmail.com` forwarding.
        </p>
      </header>

      <section className="stats-grid">
        <article className="stat-card">
          <h2>Processed Cases</h2>
          <p>{stats.casesCount}</p>
        </article>
        <article className="stat-card">
          <h2>Quarantined</h2>
          <p>{stats.quarantineCount}</p>
        </article>
        <article className="stat-card">
          <h2>Approved Senders</h2>
          <p>{stats.approvedSendersCount}</p>
        </article>
        <article className="stat-card">
          <h2>Users on Allowlist</h2>
          <p>{stats.usersWithAllowlistCount}</p>
        </article>
      </section>

      {setupError ? (
        <section className="panel panel-error">
          <div className="panel-head">
            <h2>Database Setup Required</h2>
            <p>Set `DATABASE_URL` and ensure schema is present.</p>
          </div>
          <p className="empty-state">{setupError}</p>
        </section>
      ) : null}

      <section className="panel">
        <div className="panel-head">
          <h2>Approved Senders</h2>
          <p>Only listed sender addresses are processed. Others are quarantined.</p>
        </div>

        <form className="allowlist-form" action={addApprovedSenderAction}>
          <label>
            User ID
            <input
              type="text"
              name="userId"
              placeholder="user_123"
              required
              pattern="[A-Za-z0-9._-]+"
              title="Allowed: letters, numbers, dot, underscore, dash"
            />
          </label>
          <label>
            Sender Email
            <input
              type="email"
              name="senderEmail"
              placeholder="analyst@example.com"
              required
            />
          </label>
          <button type="submit">Add Sender</button>
        </form>

        {approvedSenderGroups.length === 0 ? (
          <EmptyState text="No approved sender records yet." />
        ) : (
          <div className="allowlist-grid">
            {approvedSenderGroups.map((group) => (
              <article key={group.userId} className="allowlist-user-card">
                <h3>{group.userId}</h3>
                <p>{group.senders.length} sender(s)</p>
                <ul>
                  {group.senders.map((sender) => (
                    <li key={`${sender.userId}:${sender.senderEmail}`}>
                      <span>{sender.senderEmail}</span>
                      <form action={removeApprovedSenderAction}>
                        <input type="hidden" name="userId" value={sender.userId} />
                        <input
                          type="hidden"
                          name="senderEmail"
                          value={sender.senderEmail}
                        />
                        <button type="submit" className="danger">
                          Remove
                        </button>
                      </form>
                    </li>
                  ))}
                </ul>
              </article>
            ))}
          </div>
        )}
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2>Recent Processed Cases</h2>
          <p>Latest stored cases from Gmail API or IMAP ingestion.</p>
        </div>
        {cases.length === 0 ? (
          <EmptyState text="No processed cases yet." />
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>When</th>
                  <th>User</th>
                  <th>Sender</th>
                  <th>Mode</th>
                  <th>Subject</th>
                  <th>Scan Summary</th>
                </tr>
              </thead>
              <tbody>
                {cases.map((record) => (
                  <tr key={`${record.intakeMode}:${record.providerMessageId}`}>
                    <td>{formatDate(record.createdAt)}</td>
                    <td>{record.userId}</td>
                    <td>{record.senderEmail}</td>
                    <td>{record.intakeMode}</td>
                    <td title={record.subject}>{truncate(record.subject, 90)}</td>
                    <td>{summarizeScanResult(record.scanResultJson)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2>Recent Quarantine</h2>
          <p>Messages rejected by abuse controls or routing validation.</p>
        </div>
        {quarantine.length === 0 ? (
          <EmptyState text="No quarantined messages." />
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>When</th>
                  <th>User</th>
                  <th>Sender</th>
                  <th>Reason</th>
                  <th>Mode</th>
                  <th>Subject</th>
                </tr>
              </thead>
              <tbody>
                {quarantine.map((record) => (
                  <tr key={`${record.intakeMode}:${record.providerMessageId}`}>
                    <td>{formatDate(record.createdAt)}</td>
                    <td>{record.userId ?? "n/a"}</td>
                    <td>{record.senderEmail ?? "n/a"}</td>
                    <td>{record.reason}</td>
                    <td>{record.intakeMode}</td>
                    <td title={record.subject}>{truncate(record.subject, 90)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </main>
  );
}
