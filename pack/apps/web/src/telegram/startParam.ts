const SAFE_MISSION_ID = /^[A-Za-z0-9_-]{1,128}$/;

export function missionPathFromStartParam(startParam: string | null): string | null {
  if (!startParam || !SAFE_MISSION_ID.test(startParam)) return null;
  return `/missions/${startParam}`;
}
