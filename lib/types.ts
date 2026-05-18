export type IntakeStats = {
  casesCount: number;
  quarantineCount: number;
  approvedSendersCount: number;
  usersWithAllowlistCount: number;
};

export type CaseRecord = {
  id: number;
  intakeMode: string;
  providerMessageId: string;
  userId: string;
  senderEmail: string;
  subject: string;
  receivedAt: string;
  createdAt: string;
  scanResultJson: string;
};

export type QuarantineRecord = {
  id: number;
  intakeMode: string;
  providerMessageId: string;
  userId: string | null;
  senderEmail: string | null;
  subject: string;
  reason: string;
  receivedAt: string;
  createdAt: string;
};

export type ApprovedSender = {
  userId: string;
  senderEmail: string;
  createdAt: string;
};

export type ApprovedSenderGroup = {
  userId: string;
  senders: ApprovedSender[];
};
