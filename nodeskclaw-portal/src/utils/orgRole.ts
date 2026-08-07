// 组织角色等级比较工具：镜像后端 ADMIN_ROLE_LEVEL（app/models/org_membership.py）
export const ORG_ROLE_LEVEL: Record<string, number> = {
  member: 10,
  operator: 20,
  admin: 30,
}

export type OrgRoleName = 'member' | 'operator' | 'admin'

/** 判断 role 是否达到 minRole 及以上等级；role 为空/未知角色一律返回 false。 */
export function hasOrgRoleLevel(
  role: string | null | undefined,
  minRole: OrgRoleName,
): boolean {
  const roleLevel = role ? ORG_ROLE_LEVEL[role] ?? 0 : 0
  return roleLevel >= ORG_ROLE_LEVEL[minRole]
}
