import { dbQuery } from "@/lib/db";
import type {
  ApprovedSender,
  ApprovedSenderGroup,
  CaseRecord,
  IntakeStats,
  QuarantineRecord
} from "@/lib/types";

function toInt(value: string | number): number {
  if (typeof value === "number") {
    return value;
  }
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) ? parsed : 0;
}

export async function getStats(): Promise<IntakeStats> {
  const rows = await dbQuery<{
    cases_count: string;
    quarantine_count: string;
    approved_senders_count: string;
    users_with_allowlist_count: string;
  }>(
    `
    SELECT
      (SELECT COUNT(*) FROM cases) AS cases_count,
      (SELECT COUNT(*) FROM quarantined_messages) AS quarantine_count,
      (SELECT COUNT(*) FROM approved_senders) AS approved_senders_count,
      (SELECT COUNT(DISTINCT user_id) FROM approved_senders) AS users_with_allowlist_count
    `
  );
  const row = rows[0];
  return {
    casesCount: toInt(row?.cases_count ?? 0),
    quarantineCount: toInt(row?.quarantine_count ?? 0),
    approvedSendersCount: toInt(row?.approved_senders_count ?? 0),
    usersWithAllowlistCount: toInt(row?.users_with_allowlist_count ?? 0)
  };
}

export async function getRecentCases(limit = 50): Promise<CaseRecord[]> {
  const rows = await dbQuery<{
    id: string | number;
    intake_mode: string;
    provider_message_id: string;
    user_id: string;
    sender_email: string;
    subject: string | null;
    received_at: string | null;
    created_at: string;
    scan_result_json: string;
  }>(
    `
    SELECT
      id,
      intake_mode,
      provider_message_id,
      user_id,
      sender_email,
      subject,
      received_at,
      created_at,
      scan_result_json::text AS scan_result_json
    FROM cases
    ORDER BY created_at DESC
    LIMIT $1
    `,
    [limit]
  );
  return rows.map((row) => ({
    id: toInt(row.id),
    intakeMode: row.intake_mode,
    providerMessageId: row.provider_message_id,
    userId: row.user_id,
    senderEmail: row.sender_email,
    subject: row.subject ?? "(no subject)",
    receivedAt: row.received_at ?? "",
    createdAt: row.created_at,
    scanResultJson: row.scan_result_json
  }));
}

export async function getRecentQuarantine(limit = 50): Promise<QuarantineRecord[]> {
  const rows = await dbQuery<{
    id: string | number;
    intake_mode: string;
    provider_message_id: string;
    user_id: string | null;
    sender_email: string | null;
    subject: string | null;
    reason: string;
    received_at: string | null;
    created_at: string;
  }>(
    `
    SELECT
      id,
      intake_mode,
      provider_message_id,
      user_id,
      sender_email,
      subject,
      reason,
      received_at,
      created_at
    FROM quarantined_messages
    ORDER BY created_at DESC
    LIMIT $1
    `,
    [limit]
  );
  return rows.map((row) => ({
    id: toInt(row.id),
    intakeMode: row.intake_mode,
    providerMessageId: row.provider_message_id,
    userId: row.user_id,
    senderEmail: row.sender_email,
    subject: row.subject ?? "(no subject)",
    reason: row.reason,
    receivedAt: row.received_at ?? "",
    createdAt: row.created_at
  }));
}

export async function getApprovedSenders(): Promise<ApprovedSenderGroup[]> {
  const rows = await dbQuery<{
    user_id: string;
    sender_email: string;
    created_at: string;
  }>(
    `
    SELECT user_id, sender_email, created_at
    FROM approved_senders
    ORDER BY user_id ASC, sender_email ASC
    `
  );

  const byUser = new Map<string, ApprovedSender[]>();
  for (const row of rows) {
    const sender: ApprovedSender = {
      userId: row.user_id,
      senderEmail: row.sender_email,
      createdAt: row.created_at
    };
    if (!byUser.has(row.user_id)) {
      byUser.set(row.user_id, [sender]);
      continue;
    }
    byUser.get(row.user_id)?.push(sender);
  }

  return Array.from(byUser.entries()).map(([userId, senders]) => ({
    userId,
    senders
  }));
}

export async function addApprovedSender(userId: string, senderEmail: string): Promise<void> {
  await dbQuery(
    `
    INSERT INTO approved_senders (user_id, sender_email)
    VALUES ($1, LOWER($2))
    ON CONFLICT (user_id, sender_email) DO NOTHING
    `,
    [userId, senderEmail]
  );
}

export async function removeApprovedSender(
  userId: string,
  senderEmail: string
): Promise<void> {
  await dbQuery(
    `
    DELETE FROM approved_senders
    WHERE user_id = $1 AND sender_email = LOWER($2)
    `,
    [userId, senderEmail]
  );
}
