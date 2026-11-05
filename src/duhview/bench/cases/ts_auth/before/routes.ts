import { verifyToken } from "./auth";

type User = { name: string; role: string };

export async function deleteProject(token: string, user: User, id: string, db: { remove(id: string): void }) {
  const ok = await verifyToken(token);
  if (!ok) throw new Error("unauthorized");
  if (user.role !== "admin") throw new Error("forbidden");
  db.remove(id);
}

export function greet(el: HTMLElement, name: string) {
  el.textContent = `Hello ${name}`;
}
