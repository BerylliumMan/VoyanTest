// 038 US4：显示层密码打码
import { maskSecrets } from '../utils/maskSecrets';

describe('maskSecrets', () => {
  it('masks chinese and english passwords', () => {
    expect(maskSecrets('用户名 admin，密码 Admin@2024，查看功能')).toBe('用户名 admin，密码 ******，查看功能');
    expect(maskSecrets('password=secret')).toBe('password=******');
  });
  it('masks multiple occurrences', () => {
    const out = maskSecrets('密码 aaa 和 password: bbb');
    expect(out).not.toContain('aaa');
    expect(out).not.toContain('bbb');
    expect(out.match(/\*{6}/g)).toHaveLength(2);
  });
  it('does not touch non-password words', () => {
    expect(maskSecrets('打开密码框并输入')).toBe('打开密码框并输入');
  });
  it('handles empty', () => {
    expect(maskSecrets('')).toBe('');
    expect(maskSecrets(null)).toBe('');
  });
});
