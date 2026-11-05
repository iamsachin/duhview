export async function verifyToken(token: string): Promise<boolean> {
  const res = await fetch("https://auth.internal/verify", { method: "POST", body: token });
  return res.ok;
}
