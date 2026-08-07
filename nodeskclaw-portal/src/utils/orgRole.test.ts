import { describe, expect, it } from 'vitest'

import { hasOrgRoleLevel, ORG_ROLE_LEVEL } from './orgRole'

describe('ORG_ROLE_LEVEL', () => {
  it('member < operator < admin', () => {
    expect(ORG_ROLE_LEVEL.member).toBeLessThan(ORG_ROLE_LEVEL.operator)
    expect(ORG_ROLE_LEVEL.operator).toBeLessThan(ORG_ROLE_LEVEL.admin)
  })
})

describe('hasOrgRoleLevel', () => {
  it('member 满足 member 门槛，不满足 operator 门槛', () => {
    expect(hasOrgRoleLevel('member', 'member')).toBe(true)
    expect(hasOrgRoleLevel('member', 'operator')).toBe(false)
  })

  it('operator 满足 member/operator 门槛，不满足 admin 门槛', () => {
    expect(hasOrgRoleLevel('operator', 'member')).toBe(true)
    expect(hasOrgRoleLevel('operator', 'operator')).toBe(true)
    expect(hasOrgRoleLevel('operator', 'admin')).toBe(false)
  })

  it('admin 满足所有门槛', () => {
    expect(hasOrgRoleLevel('admin', 'member')).toBe(true)
    expect(hasOrgRoleLevel('admin', 'operator')).toBe(true)
    expect(hasOrgRoleLevel('admin', 'admin')).toBe(true)
  })

  it('null/undefined/未知角色一律不满足任何门槛', () => {
    expect(hasOrgRoleLevel(null, 'member')).toBe(false)
    expect(hasOrgRoleLevel(undefined, 'member')).toBe(false)
    expect(hasOrgRoleLevel('bogus', 'member')).toBe(false)
  })
})
