"use server";

import { revalidatePath } from "next/cache";

import { addApprovedSender, removeApprovedSender } from "@/lib/repository";

const USER_ID_RE = /^[A-Za-z0-9._-]+$/;
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

function normalizeString(value: FormDataEntryValue | null): string {
  return typeof value === "string" ? value.trim() : "";
}

export async function addApprovedSenderAction(formData: FormData): Promise<void> {
  const userId = normalizeString(formData.get("userId"));
  const senderEmail = normalizeString(formData.get("senderEmail")).toLowerCase();

  if (!USER_ID_RE.test(userId)) {
    throw new Error("userId must be alphanumeric and may include . _ -");
  }
  if (!EMAIL_RE.test(senderEmail)) {
    throw new Error("senderEmail must be a valid email address.");
  }

  await addApprovedSender(userId, senderEmail);
  revalidatePath("/");
}

export async function removeApprovedSenderAction(formData: FormData): Promise<void> {
  const userId = normalizeString(formData.get("userId"));
  const senderEmail = normalizeString(formData.get("senderEmail")).toLowerCase();

  if (!USER_ID_RE.test(userId)) {
    throw new Error("Invalid userId");
  }
  if (!EMAIL_RE.test(senderEmail)) {
    throw new Error("Invalid senderEmail");
  }

  await removeApprovedSender(userId, senderEmail);
  revalidatePath("/");
}
