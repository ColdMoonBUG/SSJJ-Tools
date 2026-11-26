/*
 * version.dll 代理（x64，无 CRT，纯 Win32）
 *
 * 目的：把换肤载荷从「跨进程 CreateRemoteThread」改成「进程内加载」。
 * 原理：SSJJ_BattleClient_Unity.exe 只导入 KERNEL32 + UnityPlayer.dll，
 *       而 UnityPlayer.dll 的导入表里有 VERSION.dll，且 version.dll 不在
 *       KnownDLLs 里 —— 所以放在 battle\*_64\ 的同名 DLL 会被优先加载。
 *       我们的代码从 DllMain 起就在进程内、跑在真实模块里，
 *       不经过 OpenProcess / VirtualAllocEx / CreateRemoteThread，
 *       GameGuard 对「外部线程执行匿名页」的拦截不会触发。
 *
 * 导出：version.dll 全部 17 个导出原样转发到 C:\Windows\System32\version.dll。
 * 载荷：risk/payload/bin/System.Configuration.Install.dll 以字节数组编进来，
 *       工作线程等 mono-2.0-bdwgc.dll 出现后调用 SsjjRisk.Loader.Load()。
 *
 * 编译：build_proxy.py（clang + lld-link，/nodefaultlib、/entry:DllMain）。
 */

typedef unsigned char u8;
typedef unsigned short u16;
typedef unsigned int u32;
typedef unsigned long long u64;
typedef int ibool;
typedef void *HANDLE;
typedef void *HMODULE;

#include <intrin.h>   /* __readgsqword：读 PEB 用，编译器内建、不引 CRT */

#define DLL_PROCESS_ATTACH 1
#define GENERIC_WRITE 0x40000000u
#define FILE_SHARE_READ 0x00000001u
#define FILE_SHARE_WRITE 0x00000002u
#define OPEN_ALWAYS 4u
#define FILE_ATTRIBUTE_NORMAL 0x00000080u
#define FILE_END 2u
#define PAGE_EXECUTE_READWRITE 0x40u

__declspec(dllimport) HMODULE __stdcall LoadLibraryA(const char *name);
__declspec(dllimport) HMODULE __stdcall GetModuleHandleW(const u16 *name);
__declspec(dllimport) void *__stdcall GetProcAddress(HMODULE m, const char *name);
__declspec(dllimport) HANDLE __stdcall CreateThread(void *sa, u32 stack, u32(__stdcall *fn)(void *), void *arg, u32 flags, u32 *tid);
__declspec(dllimport) void __stdcall Sleep(u32 ms);
__declspec(dllimport) HANDLE __stdcall CreateFileA(const char *path, u32 access, u32 share, void *sa, u32 disp, u32 attr, HANDLE tmpl);
__declspec(dllimport) u32 __stdcall SetFilePointer(HANDLE h, long lo, long *hi, u32 method);
__declspec(dllimport) ibool __stdcall WriteFile(HANDLE h, const void *buf, u32 len, u32 *written, void *ov);
__declspec(dllimport) ibool __stdcall CloseHandle(HANDLE h);
__declspec(dllimport) u32 __stdcall GetEnvironmentVariableA(const char *name, char *buf, u32 size);
__declspec(dllimport) HMODULE __stdcall GetModuleHandleA(const char *name);
__declspec(dllimport) u32 __stdcall GetModuleFileNameW(HMODULE m, u16 *buf, u32 size);
__declspec(dllimport) ibool __stdcall MoveFileW(const u16 *from, const u16 *to);
__declspec(dllimport) ibool __stdcall MoveFileExW(const u16 *from, const u16 *to, u32 flags);
__declspec(dllimport) u32 __stdcall GetLastError(void);
__declspec(dllimport) u32 __stdcall GetEnvironmentVariableW(const u16 *name, u16 *buf, u32 size);
__declspec(dllimport) HANDLE __stdcall CreateFileW(const u16 *path, u32 access, u32 share, void *sa, u32 disp, u32 attr, HANDLE tmpl);
__declspec(dllimport) int __stdcall MultiByteToWideChar(u32 cp, u32 flags, const char *src, int srclen, u16 *dst, int dstlen);
__declspec(dllimport) ibool __stdcall DeleteFileW(const u16 *path);
__declspec(dllimport) u32 __stdcall GetCurrentProcessId(void);

#include "payload_dll.h"

/* ---------------- 隐藏自身模块（PEB 摘链） ----------------
   GameGuard / 游戏自带 RuntimeCheatGuard 都是用「枚举进程模块」来判断进程里有没有
   多出来的 DLL。模块枚举走的是 PEB->Ldr 的三条双向链表，把本模块从链表里摘掉，
   枚举就看不见我们了（代码已经映射，IAT 也已经绑好，摘链不影响运行）。
   注意：DLL_PROCESS_ATTACH 期间加载器正在遍历 InInitializationOrderModuleList，
   那时只能摘 InLoadOrder / InMemoryOrder 两条，第三条留到定时器回调里再摘。 */

typedef struct _LIST_ENTRY2 {
    struct _LIST_ENTRY2 *Flink;
    struct _LIST_ENTRY2 *Blink;
} LIST_ENTRY2;

static void plog(const char *msg);
static void plog2(const char *a, const char *b);

typedef struct _UNI_STR {
    u16 Length;
    u16 MaxLength;
    u16 *Buffer;
} UNI_STR;

typedef struct _LDR_ENTRY {
    LIST_ENTRY2 InLoadOrderLinks;
    LIST_ENTRY2 InMemoryOrderLinks;
    LIST_ENTRY2 InInitializationOrderLinks;
    void *DllBase;
    void *EntryPoint;
    u32 SizeOfImage;
    u32 pad0;
    UNI_STR FullDllName;
    UNI_STR BaseDllName;
} LDR_ENTRY;

typedef struct _PEB_LDR2 {
    u32 Length;
    u32 Initialized;
    void *SsHandle;
    LIST_ENTRY2 InLoadOrderModuleList;
    LIST_ENTRY2 InMemoryOrderModuleList;
    LIST_ENTRY2 InInitializationOrderModuleList;
} PEB_LDR2;

static void unlink_entry(LIST_ENTRY2 *l)
{
    if (!l->Flink || !l->Blink || l->Flink == l) return;
    l->Blink->Flink = l->Flink;
    l->Flink->Blink = l->Blink;
    l->Flink = l;
    l->Blink = l;
}

static void plog_n(const char *a, u32 v)
{
    char buf[128];
    int n = 0, k = 0;
    while (a[n] && n < 96) { buf[n] = a[n]; n++; }
    char tmp[16];
    if (v == 0) tmp[k++] = '0';
    while (v > 0) { tmp[k++] = (char)('0' + (v % 10)); v /= 10; }
    while (k > 0) buf[n++] = tmp[--k];
    buf[n] = 0;
    plog(buf);
}

/* DllMain 里找到就记下来：两条链摘掉之后 InLoadOrder 遍历不到自己了，
   第 3 条链只能靠这个指针直接摘。 */
static void *g_entry;

/* 在三条链表里找 DllBase == self 的条目；mask=1 只摘前两条，mask=7 三条全摘。
   返回摘掉的链表条数。 */
static int cloak_module(HMODULE self, int full)
{
    u64 peb = __readgsqword(0x60);
    if (!peb) { plog("[proxy] 读不到 PEB"); return 0; }
    void *ldr = *(void **)(u64)(peb + 0x18);
    if (!ldr) { plog("[proxy] PEB->Ldr 为空"); return 0; }
    PEB_LDR2 *p = (PEB_LDR2 *)ldr;

    LIST_ENTRY2 *head = &p->InLoadOrderModuleList;
    LIST_ENTRY2 *cur = head->Flink;
    int guard = 0;
    while (cur && cur != head && guard++ < 4096) {
        LDR_ENTRY *e = (LDR_ENTRY *)cur;
        if (e->DllBase == (void *)self) {
            int n = 0;
            g_entry = (void *)e;
            unlink_entry(&e->InLoadOrderLinks);
            n++;
            unlink_entry(&e->InMemoryOrderLinks);
            n++;
            if (full) {
                unlink_entry(&e->InInitializationOrderLinks);
                n++;
            }
            return n;
        }
        cur = cur->Flink;
    }
    {
        char buf[40];
        int i = 0;
        buf[i++] = '0' + (char)((guard / 100) % 10);
        buf[i++] = '0' + (char)((guard / 10) % 10);
        buf[i++] = '0' + (char)(guard % 10);
        buf[i] = 0;
        plog2("[proxy] PEB 里没找到本模块（走过条目数=", buf);
    }
    return 0;
}

/* ---------------- 小工具 ---------------- */

static u32 slen(const char *s) { u32 n = 0; while (s && s[n]) n++; return n; }

static void a2w(const char *src, u16 *dst, int cap)
{
    int i = 0;
    while (src && src[i] && i < cap - 1) { dst[i] = (u16)(unsigned char)src[i]; i++; }
    dst[i] = 0;
}

/* 把本模块在 LDR 条目里的完整路径就地改写成系统 version.dll 的路径。
   游戏自带 RuntimeCheatGuard 判定「进程里有没有可疑代理」的做法是：拿到某个模块
   再取它的路径，看是不是在 System32 之外。路径一改，它就认为这是系统那份。
   只改字符串内容（原缓冲容量够大，系统路径更短），不动链表、不动文件句柄，
   所以 DllMain 里就能安全执行。真实路径在改写前已存进 g_real_path 给 hide_self 用。 */
static int spoof_names(HMODULE self)
{
    u64 peb = __readgsqword(0x60);
    if (!peb) return 0;
    void *ldr = *(void **)(u64)(peb + 0x18);
    if (!ldr) return 0;
    PEB_LDR2 *p = (PEB_LDR2 *)ldr;
    LIST_ENTRY2 *head = &p->InLoadOrderModuleList;
    LIST_ENTRY2 *cur = head->Flink;
    int guard = 0;
    while (cur && cur != head && guard++ < 4096) {
        LDR_ENTRY *e = (LDR_ENTRY *)cur;
        if (e->DllBase == (void *)self) {
            const char *fake = "C:\\Windows\\System32\\version.dll";
            int L = (int)slen(fake);
            if (!e->FullDllName.Buffer) return -1;
            if ((int)e->FullDllName.MaxLength < (L + 1) * 2) return -2;
            a2w(fake, e->FullDllName.Buffer, (int)e->FullDllName.MaxLength / 2);
            e->FullDllName.Length = (u16)(L * 2);
            return 1;
        }
        cur = cur->Flink;
    }
    return 0;
}

static void cat(char *dst, int cap, const char *a, const char *b)
{
    int i = 0, j = 0;
    while (a && a[i] && i < cap - 1) { dst[i] = a[i]; i++; }
    while (b && b[j] && i < cap - 1) { dst[i] = b[j]; i++; j++; }
    dst[i] = 0;
}

static void u16cat(u16 *dst, int cap, const u16 *a, const u16 *b)
{
    int i = 0, j = 0;
    while (a && a[i] && i < cap - 1) { dst[i] = a[i]; i++; }
    while (b && b[j] && i < cap - 1) { dst[i] = b[j]; i++; j++; }
    dst[i] = 0;
}

/* 日志路径用宽字符：用户目录带中文（如 C:\\Users\\张三\\AppData\\...）时，
   原来的 CreateFileA 会按 ANSI 代码页去解释 UTF-8 字节，建文件失败 → 整局 proxy.log 0 行，
   现场就只剩「游戏自己退出」一个现象，没法定位。 */
static u16 g_logw[520];
static ibool g_log_ready = 0;

static void open_log(void)
{
    if (g_log_ready) return;
    u16 base[400];
    u32 n = GetEnvironmentVariableW(L"LOCALAPPDATA", base, 400);
    if (n == 0 || n >= 400) {
        /* 极端情况（服务账户没有 LOCALAPPDATA）：退到 C:\\Windows\\Temp */
        u16cat(g_logw, 520, L"C:\\Windows\\Temp", L"\\ssjj_proxy.log");
        g_log_ready = 1;
        return;
    }
    u16cat(g_logw, 520, base, L"\\ssjj-toolbox\\proxy.log");
    g_log_ready = 1;
}

static void plog(const char *msg)
{
    if (!g_log_ready) open_log();
    if (!g_log_ready) return;
    /* 内容仍是 UTF-8 字节（工具箱的诊断包按 UTF-8 直接读），只有「路径」用宽字符：
       中文用户名（C:\\Users\\张三\\AppData\\...）下 CreateFileA 会按 ANSI 解释
       UTF-8 字节，建文件直接失败 → 整局 proxy.log 0 行，现场什么都看不到。 */
    HANDLE h = CreateFileW(g_logw, 0x00000004u /*FILE_APPEND_DATA*/, FILE_SHARE_READ | FILE_SHARE_WRITE, 0,
                           OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, 0);
    if (h == (HANDLE)-1 || !h) return;
    u32 wr = 0;
    WriteFile(h, msg, slen(msg), &wr, 0);
    WriteFile(h, "\r\n", 2, &wr, 0);
    CloseHandle(h);
}

static void plog2(const char *a, const char *b)
{
    char buf[512];
    cat(buf, (int)sizeof(buf), a, b);
    plog(buf);
}

/* ---------------- version.dll 转发 ---------------- */

static HMODULE g_sys;
static char g_sys_path[] = "C:\\Windows\\System32\\version.dll";

static HMODULE g_self;
static u16 g_real_path[520];   /* DllMain 里抢在路径伪装之前存下来的真实路径 */
static int g_spoof;            /* 路径伪装结果：1/0/-1/-2 */
static int g_cloak_boot;       /* DllMain 摘链结果 */
static HMODULE g_sys_pinned;   /* DllMain 里先钉住的系统 version.dll，防止伪装后被当成本模块 */

static void *sysfn(const char *name)
{
    if (!g_sys) {
        /* 路径伪装后 GetModuleHandleA(系统路径) 可能反过来命中我们自己（我们现在的 LDR 路径
           就是系统路径），那样 GetProcAddress 拿到的是本文件的转发函数 → 无限递归。
           所以 DllMain 里先 LoadLibrary 钉住真正的系统 DLL，这里直接用。 */
        g_sys = g_sys_pinned;
        if (!g_sys) g_sys = GetModuleHandleA(g_sys_path);
        if (!g_sys) g_sys = LoadLibraryA(g_sys_path);
        if (g_sys == g_self) g_sys = 0;   /* 兜底：万一命中的是自己，宁可不要 */
        if (!g_sys) g_sys = LoadLibraryA(g_sys_path);
    }
    if (!g_sys) return 0;
    return GetProcAddress(g_sys, name);
}

typedef ibool(__stdcall *fn_vqw)(const void *, const u16 *, void **, u32 *);
typedef ibool(__stdcall *fn_gfviw)(const u16 *, u32, u32, void *);
typedef u32(__stdcall *fn_gfvisw)(const u16 *, u32 *);
typedef u32(__stdcall *fn_verb)(const void *, const u16 *, void **, u32 *);

__declspec(dllexport) ibool __stdcall VerQueryValueW(const void *block, const u16 *sub, void **buf, u32 *len)
{
    fn_vqw p = (fn_vqw)sysfn("VerQueryValueW");
    return p ? p(block, sub, buf, len) : 0;
}
__declspec(dllexport) ibool __stdcall VerQueryValueA(const void *block, const char *sub, void **buf, u32 *len)
{
    fn_vqw p = (fn_vqw)sysfn("VerQueryValueA");
    return p ? p(block, (const u16 *)sub, buf, len) : 0;
}
__declspec(dllexport) ibool __stdcall GetFileVersionInfoW(const u16 *f, u32 hs, u32 len, void *data)
{
    fn_gfviw p = (fn_gfviw)sysfn("GetFileVersionInfoW");
    return p ? p(f, hs, len, data) : 0;
}
__declspec(dllexport) ibool __stdcall GetFileVersionInfoA(const char *f, u32 hs, u32 len, void *data)
{
    fn_gfviw p = (fn_gfviw)sysfn("GetFileVersionInfoA");
    return p ? p((const u16 *)f, hs, len, data) : 0;
}
__declspec(dllexport) ibool __stdcall GetFileVersionInfoExW(u32 flags, const u16 *f, u32 hs, u32 len, void *data)
{
    typedef ibool(__stdcall *fn)(u32, const u16 *, u32, u32, void *);
    fn p = (fn)sysfn("GetFileVersionInfoExW");
    return p ? p(flags, f, hs, len, data) : 0;
}
__declspec(dllexport) ibool __stdcall GetFileVersionInfoExA(u32 flags, const char *f, u32 hs, u32 len, void *data)
{
    typedef ibool(__stdcall *fn)(u32, const char *, u32, u32, void *);
    fn p = (fn)sysfn("GetFileVersionInfoExA");
    return p ? p(flags, f, hs, len, data) : 0;
}
__declspec(dllexport) u32 __stdcall GetFileVersionInfoSizeW(const u16 *f, u32 *handle)
{
    fn_gfvisw p = (fn_gfvisw)sysfn("GetFileVersionInfoSizeW");
    return p ? p(f, handle) : 0;
}
__declspec(dllexport) u32 __stdcall GetFileVersionInfoSizeA(const char *f, u32 *handle)
{
    fn_gfvisw p = (fn_gfvisw)sysfn("GetFileVersionInfoSizeA");
    return p ? p((const u16 *)f, handle) : 0;
}
__declspec(dllexport) u32 __stdcall GetFileVersionInfoSizeExW(u32 flags, const u16 *f, u32 *handle)
{
    typedef u32(__stdcall *fn)(u32, const u16 *, u32 *);
    fn p = (fn)sysfn("GetFileVersionInfoSizeExW");
    return p ? p(flags, f, handle) : 0;
}
__declspec(dllexport) u32 __stdcall GetFileVersionInfoSizeExA(u32 flags, const char *f, u32 *handle)
{
    typedef u32(__stdcall *fn)(u32, const char *, u32 *);
    fn p = (fn)sysfn("GetFileVersionInfoSizeExA");
    return p ? p(flags, f, handle) : 0;
}
__declspec(dllexport) ibool __stdcall GetFileVersionInfoByHandle(u32 flags, HANDLE h, u32 len, void *data)
{
    typedef ibool(__stdcall *fn)(u32, HANDLE, u32, void *);
    fn p = (fn)sysfn("GetFileVersionInfoByHandle");
    return p ? p(flags, h, len, data) : 0;
}
__declspec(dllexport) u32 __stdcall VerFindFileA(u32 flags, const char *file, const char *winDir, const char *appDir,
                                                char *curDir, u32 *curLen, char *destDir, u32 *destLen)
{
    typedef u32(__stdcall *fn)(u32, const char *, const char *, const char *, char *, u32 *, char *, u32 *);
    fn p = (fn)sysfn("VerFindFileA");
    return p ? p(flags, file, winDir, appDir, curDir, curLen, destDir, destLen) : 0;
}
__declspec(dllexport) u32 __stdcall VerFindFileW(u32 flags, const u16 *file, const u16 *winDir, const u16 *appDir,
                                                u16 *curDir, u32 *curLen, u16 *destDir, u32 *destLen)
{
    typedef u32(__stdcall *fn)(u32, const u16 *, const u16 *, const u16 *, u16 *, u32 *, u16 *, u32 *);
    fn p = (fn)sysfn("VerFindFileW");
    return p ? p(flags, file, winDir, appDir, curDir, curLen, destDir, destLen) : 0;
}
__declspec(dllexport) u32 __stdcall VerInstallFileA(u32 flags, const char *src, const char *dest, const char *srcDir,
                                                   const char *destDir, char *tmp, u32 *tmpLen)
{
    typedef u32(__stdcall *fn)(u32, const char *, const char *, const char *, const char *, char *, u32 *);
    fn p = (fn)sysfn("VerInstallFileA");
    return p ? p(flags, src, dest, srcDir, destDir, tmp, tmpLen) : 0;
}
__declspec(dllexport) u32 __stdcall VerInstallFileW(u32 flags, const u16 *src, const u16 *dest, const u16 *srcDir,
                                                   const u16 *destDir, u16 *tmp, u32 *tmpLen)
{
    typedef u32(__stdcall *fn)(u32, const u16 *, const u16 *, const u16 *, const u16 *, u16 *, u32 *);
    fn p = (fn)sysfn("VerInstallFileW");
    return p ? p(flags, src, dest, srcDir, destDir, tmp, tmpLen) : 0;
}
__declspec(dllexport) u32 __stdcall VerLanguageNameA(u32 lang, char *buf, u32 len)
{
    typedef u32(__stdcall *fn)(u32, char *, u32);
    fn p = (fn)sysfn("VerLanguageNameA");
    return p ? p(lang, buf, len) : 0;
}
__declspec(dllexport) u32 __stdcall VerLanguageNameW(u32 lang, u16 *buf, u32 len)
{
    typedef u32(__stdcall *fn)(u32, u16 *, u32);
    fn p = (fn)sysfn("VerLanguageNameW");
    return p ? p(lang, buf, len) : 0;
}

/* ---------------- mono 调用 ---------------- */

/* 把本 DLL 的文件名从 version.dll 改成 .ver.dat（同目录）。
   GetModuleFileNameW(self) 给出的是这一份的完整路径，改完游戏目录里就没有
   「exe 旁边多出来的 version.dll」这个特征了。失败不影响运行，只记一行日志。 */
static int hide_self(HMODULE self)
{
    /* 返回：1=本次改名成功；0=已经被别的进程改过（错误码 2）；<0 = 失败（绝对值带 GetLastError） */
    /* 注意：不能用 GetModuleFileNameW —— DllMain 里已把 LDR 里的路径伪装成系统 version.dll，
       那个 API 读的就是这条路径，会去改名 System32 下的文件（必然失败）。用 g_real_path。 */
    u16 path[520];
    u32 n = 0;
    if (g_real_path[0]) {
        while (g_real_path[n] && n < 519) { path[n] = g_real_path[n]; n++; }
        path[n] = 0;
    } else {
        n = GetModuleFileNameW(self, path, 520);
    }
    if (n == 0 || n >= 520) return -2;
    int cut = -1, k;
    for (k = 0; k < (int)n; k++) if (path[k] == '\\') cut = k;
    if (cut < 0) return -2;

    /* 先清理历史槽位：残留的 .ver.dat / .ver1~9.dat 会把 10 个名字占满，
       导致这次改名直接 -1999（其它机器上就是「过检测失效、之后一直进不去」的成因之一）。
       删不掉的（还被上一局进程映射着）跳过即可，DeleteFileW 失败不影响后面。 */
    {
        int ci;
        for (ci = 0; ci < 10; ci++) {
            u16 tmp[520];
            int kk;
            for (kk = 0; kk <= cut; kk++) tmp[kk] = path[kk];
            const char *p2 = ".ver";
            int t2 = 0;
            while (p2[t2]) { tmp[cut + 1 + t2] = (u16)(unsigned char)p2[t2]; t2++; }
            if (ci > 0) tmp[cut + 1 + t2++] = (u16)('0' + ci);
            const char *s2 = ".dat";
            int u2 = 0;
            while (s2[u2]) { tmp[cut + 1 + t2] = (u16)(unsigned char)s2[u2]; u2++; t2++; }
            tmp[cut + 1 + t2] = 0;
            DeleteFileW(tmp);
        }
    }

    /* 依次尝试 .ver.dat / .ver1.dat ... .ver9.dat：上一局留下的 .ver.dat 可能被别的进程
       锁着（实测 MoveFileEx REPLACE_EXISTING 也报 1005 Access denied），换个名字绕开。 */
    int idx;
    for (idx = 0; idx < 10; idx++)
    {
        u16 dst[520];
        for (k = 0; k <= cut; k++) dst[k] = path[k];
        const char *pre = ".ver";
        int t = 0;
        while (pre[t]) { dst[cut + 1 + t] = (u16)(unsigned char)pre[t]; t++; }
        if (idx > 0) dst[cut + 1 + t++] = (u16)('0' + idx);
        const char *suf = ".dat";
        int u = 0;
        while (suf[u]) { dst[cut + 1 + t] = (u16)(unsigned char)suf[u]; u++; t++; }
        dst[cut + 1 + t] = 0;

        if (MoveFileExW(path, dst, 0x8)) return 1;   /* 不覆盖：目标已被占就换下一个名字 */
        u32 e = GetLastError();
        if (e == 2) return 0;            /* 源文件已经不在原名上，说明别人改过了 */
        if (e == 183) continue;          /* 目标已存在，换下一个名字 */
        if (e == 5 || e == 32) continue;  /* 目标被锁，换下一个名字 */
        return -(int)(1000 + e);
    }
    return -1999;
}

typedef void *(*fn_root)(void);
typedef void *(*fn_attach)(void *);
typedef void *(*fn_open)(char *, u32, int, int *, int, const char *);
typedef void *(*fn_loadfull)(void *, const char *, int *, int);
typedef void *(*fn_class)(void *, const char *, const char *);
typedef void *(*fn_method)(void *, const char *, int);
typedef void *(*fn_invoke)(void *, void *, void **, void **);

/* 真正干活的那一段：mono 就位后把载荷灌进 AppDomain 并调 Loader.Load()。 */
static void run_payload(HMODULE mono)
{
    fn_root p_root = (fn_root)GetProcAddress(mono, "mono_get_root_domain");
    fn_attach p_attach = (fn_attach)GetProcAddress(mono, "mono_thread_attach");
    fn_open p_open = (fn_open)GetProcAddress(mono, "mono_image_open_from_data_with_name");
    fn_loadfull p_load = (fn_loadfull)GetProcAddress(mono, "mono_assembly_load_from_full");
    fn_class p_class = (fn_class)GetProcAddress(mono, "mono_class_from_name");
    fn_method p_method = (fn_method)GetProcAddress(mono, "mono_class_get_method_from_name");
    fn_invoke p_invoke = (fn_invoke)GetProcAddress(mono, "mono_runtime_invoke");

    if (!p_root || !p_attach || !p_open || !p_load || !p_class || !p_method || !p_invoke) {
        plog("[proxy] mono 导出缺失，放弃");
        return;
    }

    void *domain = p_root();
    if (!domain) { plog("[proxy] root domain 为空"); return; }
    p_attach(domain);

    int status = 0;
    void *image = p_open((char *)payload_dll, payload_dll_len, 1, &status, 0, PAYLOAD_NAME);
    if (!image) { plog("[proxy] mono_image_open 失败"); return; }

    status = 0;
    void *asm_ = p_load(image, PAYLOAD_NAME, &status, 0);
    if (!asm_) { plog("[proxy] mono_assembly_load_from_full 失败"); return; }

    void *klass = p_class(image, "SsjjRisk", "Loader");
    if (!klass) { plog("[proxy] 找不到 SsjjRisk.Loader"); return; }

    void *method = p_method(klass, "Load", 0);
    if (!method) { plog("[proxy] 找不到 Loader.Load()"); return; }

    plog("[proxy] 调用 SsjjRisk.Loader.Load()");
    void *exc = 0;
    p_invoke(method, 0, 0, &exc);
    if (exc) plog("[proxy] Loader.Load 抛异常（详见 risk_inject.log）");
    else plog("[proxy] Loader.Load 返回");
}

/* ---------------- 定时器驱动（不创建任何线程） ----------------
   实测证据（cdb 分析 23:56 的 dump）：进程被杀时崩溃栈是
     ntdll!LdrpInitializeThread → LdrpCallInitRoutine → … → VERSION!…
   即 GameGuard 是在「我们的模块创建了一个新线程、新线程开始做 DLL 初始化」的
   那一刻下手的（远程线程注入那次也是死在 CreateRemoteThread）。所以这里彻底
   不建线程：DllMain 里 SetTimer，回调跑在进程原有的主线程消息循环上。 */

typedef u64(__stdcall *fn_settimer)(void *, u64, u32, void *);
typedef ibool(__stdcall *fn_killtimer)(void *, u64);

static int g_step;
static volatile long g_done;
static volatile long g_renamed;
static fn_killtimer g_killtimer;
static u64 g_timer_id;          /* SetTimer(hWnd=0) 的真实返回值，KillTimer 必须用它 */

static void stop_timer(void)
{
    if (g_killtimer && g_timer_id) { g_killtimer(0, g_timer_id); g_timer_id = 0; }
}

/* 第 3 条链（InInitializationOrder）只能靠 DllMain 记下的条目指针去摘 */
static void unlink_init_chain(void)
{
    if (!g_entry) { plog("[proxy] 没记录 LDR 条目，跳过第 3 条链"); return; }
    LDR_ENTRY *e = (LDR_ENTRY *)g_entry;
    unlink_entry(&e->InInitializationOrderLinks);
    plog("[proxy] 已摘 InInitializationOrder 链（三条链全部摘除）");
}

static void __stdcall on_timer(void *wnd, u32 msg, u64 id, u32 tick)
{
    (void)wnd; (void)msg; (void)id; (void)tick;
    if (g_done) return;          /* invoke 期间积压的 WM_TIMER 会重入，直接忽略 */
    g_step++;

    /* 1) 改名：每 25ms 试一次，最多 80 次（2 秒）。游戏自检在 ~90ms 扫 exe 旁文件，
          正常情况下第 1~2 次就能改掉；杀软刚扫过新拷进来的 DLL 时会占几百毫秒，靠重试兜住。 */
    if (!g_renamed && g_step <= 80) {
        int hr = hide_self(g_self);
        if (hr == 1) { g_renamed = 1; open_log(); plog("[proxy] 已把自身文件改名为 .ver.dat（躲开目录扫描）"); }
        else if (hr == 0) { g_renamed = 1; open_log(); plog("[proxy] 自身文件已是 .ver.dat（别的进程改的）"); }
        else {
            if (g_step <= 3 || g_step % 20 == 0) { open_log(); plog_n("[proxy] 改名失败 hr=", (u32)(-hr)); }
            if (g_step == 80) { open_log(); plog("[proxy] 自身文件改名始终失败（目录里仍是 version.dll）"); }
        }
    }

    /* 2) 摘 PEB 链：等加载器把初始化走完再做（大约在 100ms 之后），否则会打断 LdrpInitializeThread */
    if (g_step == 1) {
        open_log();
        plog("[proxy] 定时器回调已进入（没有创建线程）");
        plog_n("[proxy] DllMain 路径伪装=", (u32)(g_spoof < 0 ? 90 : g_spoof));
        plog_n("[proxy] DllMain 摘链条数=", (u32)g_cloak_boot);
        plog_n("[proxy] 系统 version.dll 句柄=", (u32)(g_sys_pinned != 0));
        unlink_init_chain();
    }

    /* 3) 等 mono 起来，把载荷灌进去 */
    HMODULE mono = GetModuleHandleA("mono-2.0-bdwgc.dll");
    if (!mono) {
        if (g_step > 2400) { open_log(); plog("[proxy] 等不到 mono-2.0-bdwgc.dll，放弃"); stop_timer(); }
        return;
    }
    if (g_step < 1) return;
    if (_InterlockedCompareExchange(&g_done, 1, 0) != 0) { stop_timer(); return; }
    open_log();
    plog("[proxy] mono 模块已就位");
    stop_timer();
    run_payload(mono);
}

/* SetTimer 拿不到时的兜底（会创建线程，只在上面的路走不通时用） */
/* 兜底线程：始终创建。实测有机器在 -window-mode exclusive 下主线程跑了近 3 秒
   也没派发过一次 WM_TIMER，只靠定时器会「改名没做、载荷没加载、proxy.log 0 行」。
   这里只做三件事：抢改名、补摘第 3 条链、等 mono 后灌载荷（与定时器路径用 g_done 原子互斥）。 */
static u32 __stdcall worker(void *arg)
{
    (void)arg;
    open_log();
    plog("[proxy] 后台线程启动（定时器之外的兜底路径）");
    int i;
    for (i = 0; i < 200 && !g_renamed; i++) {
        int hr = hide_self(g_self);
        if (hr == 1) { g_renamed = 1; plog("[proxy] 已把自身文件改名为 .ver.dat（线程路径）"); break; }
        if (hr == 0) { g_renamed = 1; plog("[proxy] 自身文件已是 .ver.dat（别的进程改的）"); break; }
        Sleep(25);
    }
    if (!g_renamed) plog("[proxy] 线程路径改名未成功（目录里可能仍是 version.dll）");
    unlink_init_chain();
    for (i = 0; i < 2400; i++) {
        if (g_done) return 0;
        HMODULE mono = GetModuleHandleA("mono-2.0-bdwgc.dll");
        if (mono) {
            /* 等待 Mono 完成 root_domain 初始化：避免在刚映射进进程时立刻调 mono_thread_attach 触发 0xc0000409，
               同时以 10ms 高频轮询且不做额外延迟，一旦 root_domain 非空立即抢在 npggNT64.des 激活前注入。 */
            fn_root p_root = (fn_root)GetProcAddress(mono, "mono_get_root_domain");
            if (!p_root) {
                plog("[proxy] 找不到 mono_get_root_domain（线程路径），放弃");
                return 0;
            }
            int j;
            void *dom = 0;
            for (j = 0; j < 1500; j++) {
                if (g_done) return 0;
                dom = p_root();
                if (dom) break;
                Sleep(10);
            }
            if (!dom) {
                plog("[proxy] 等待 mono root_domain 超时（线程路径），放弃");
                return 0;
            }
            if (_InterlockedCompareExchange(&g_done, 1, 0) == 0) {
                plog("[proxy] mono root_domain 已就位，立即执行注入（线程路径）");
                run_payload(mono);
            }
            return 0;
        }
        Sleep(25);
    }
    plog("[proxy] 等不到 mono-2.0-bdwgc.dll，放弃");
    return 0;
}

static u16 to_lower_ascii(u16 c)
{
    return (c >= 'A' && c <= 'Z') ? (u16)(c + ('a' - 'A')) : c;
}

/* 仅允许在战斗端主进程 SSJJ_BattleClient_Unity.exe 中激活代理逻辑。
   同目录的 UnityCrashHandler64.exe 等辅助进程也会加载本地 version.dll；
   若不过滤宿主进程，它们会抢先执行 hide_self() 把 version.dll 改名为 .ver.dat，
   导致真正的战斗端随后找不到本地 DLL 而回退系统 System32\version.dll，换肤彻底失效。 */
static int is_battle_client_host(void)
{
    u16 exe[520];
    u32 n = GetModuleFileNameW(0, exe, 520);
    if (n == 0 || n >= 520) return 0;
    int start = 0, i;
    for (i = 0; i < (int)n; i++) {
        if (exe[i] == '\\' || exe[i] == '/') start = i + 1;
    }
    const char *want = "ssjj_battleclient_unity.exe";
    int j = 0;
    while (want[j] && exe[start + j]) {
        if (to_lower_ascii(exe[start + j]) != (u16)(unsigned char)want[j]) return 0;
        j++;
    }
    return want[j] == 0 && exe[start + j] == 0;
}

static int get_host_basename_utf8(char *out, int cap)
{
    u16 exe[520];
    u32 n = GetModuleFileNameW(0, exe, 520);
    if (n == 0 || n >= 520) {
        if (cap > 0) out[0] = 0;
        return 0;
    }
    int start = 0, i;
    for (i = 0; i < (int)n; i++) {
        if (exe[i] == '\\' || exe[i] == '/') start = i + 1;
    }
    int j = 0;
    while (exe[start + j] && j < cap - 1) {
        u16 c = exe[start + j];
        out[j] = (c < 128) ? (char)c : '?';
        j++;
    }
    out[j] = 0;
    return j;
}

static void log_attach(u32 pid, int match, const char *host)
{
    char buf[256];
    int n = 0, k = 0;
    const char *p1 = "[proxy] ATTACH pid=";
    while (p1[k] && n < 240) buf[n++] = p1[k++];
    char tmp[16];
    int tk = 0;
    if (pid == 0) tmp[tk++] = '0';
    while (pid > 0) { tmp[tk++] = (char)('0' + (pid % 10)); pid /= 10; }
    while (tk > 0 && n < 240) buf[n++] = tmp[--tk];
    const char *p2 = match ? " match=1 host=" : " match=0 host=";
    k = 0;
    while (p2[k] && n < 240) buf[n++] = p2[k++];
    k = 0;
    while (host && host[k] && n < 254) buf[n++] = host[k++];
    buf[n] = 0;
    open_log();
    plog(buf);
}

ibool __stdcall DllMain(HANDLE mod, u32 reason, void *reserved)
{
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        g_self = (HMODULE)mod;
        int is_battle = is_battle_client_host();
        {
            char host[128];
            get_host_basename_utf8(host, (int)sizeof(host));
            log_attach(GetCurrentProcessId(), is_battle, host[0] ? host : "?");
        }
        if (!is_battle) {
            /* 非战斗端进程（如 UnityCrashHandler64.exe）：仅加载系统 version.dll 做纯 API 转发透传，
               严禁改名、严禁摘链、严禁挂定时器、严禁起后台线程。 */
            g_sys_pinned = LoadLibraryA("C:\\Windows\\System32\\version.dll");
            return 1;
        }
        /* 改名必须赶在游戏自己的自检扫描之前：它在启动 ~90ms 就枚举 exe 旁文件并上报
           「Suspicious proxy DLL exists beside game exe」。定时器第一次回调要 200ms，
           太晚；DllMain 里只做一次 MoveFile，代价是一次文件元数据操作，可以接受。 */
        /* 注意：DllMain 阶段不能 MoveFile —— 此刻加载器还开着镜像文件句柄（无 share-delete），
           改名必然失败。所以改名放在定时器第一次回调（25ms），仍早于游戏 ~90ms 的自检扫描。 */
        /* 但「从 PEB 模块链上摘掉自己」不碰文件句柄，DllMain 里就能做，而且是必须做：
           游戏自带 RuntimeCheatGuard 在启动约 30~40ms 就枚举进程模块并上报
           「Suspicious proxy DLL is loaded by game process」，比定时器第一次回调还早，
           等定时器来摘就晚了 30 毫秒（实测 12:54:47.766 判定 / .800 才轮到我们的载荷）。
           这里只摘 InLoadOrder + InMemoryOrder（模块枚举走的就是这两条链），
           InInitializationOrder 留给加载器，避免打断 LdrpInitializeThread 的初始化遍历。 */
        {
            /* 顺序很关键：
               1) 先取真实路径（伪装之后就取不到了）；
               2) 再 LoadLibrary 钉住系统 version.dll（伪装之后按路径查会命中自己）；
               3) 伪装 LDR 里的路径；
               4) 最后摘 InLoadOrder / InMemoryOrder 链。 */
            u32 rn = GetModuleFileNameW((HMODULE)mod, g_real_path, 520);
            if (rn == 0 || rn >= 520) g_real_path[0] = 0;
            g_sys_pinned = LoadLibraryA("C:\\Windows\\System32\\version.dll");
            g_spoof = spoof_names((HMODULE)mod);
            g_cloak_boot = cloak_module((HMODULE)mod, 0);
        }
        /* DllMain 里就把第一行写进日志：以前的版本一旦定时器没派发，proxy.log 就是 0 行，
           现场只能看到「游戏自己退出」，根本没法判断是改名失败还是根本没进来。 */
        open_log();
        plog_n("[proxy] DllMain 进入 pid=", GetCurrentProcessId());
        plog_n("[proxy] 路径伪装=", (u32)(g_spoof < 0 ? 90 : g_spoof));

        HMODULE u = GetModuleHandleA("user32.dll");
        fn_settimer pset = u ? (fn_settimer)GetProcAddress(u, "SetTimer") : 0;
        g_killtimer = u ? (fn_killtimer)GetProcAddress(u, "KillTimer") : 0;
        if (pset) {
            u64 id = (u64)pset(0, 1, 25, (void *)on_timer);
            g_timer_id = id;
            if (!id) plog("[proxy] SetTimer 失败，只靠后台线程");
        } else {
            plog("[proxy] 取不到 SetTimer，只靠后台线程");
        }
        /* 定时器与线程两条路都开：定时器没派发时线程顶上，线程被拦时定时器顶上，
           两边用 g_done / g_renamed 互斥，不会重复加载载荷。 */
        {
            u32 tid = 0;
            CreateThread(0, 0, worker, mod, 0, &tid);
        }
    }
    return 1;
}
