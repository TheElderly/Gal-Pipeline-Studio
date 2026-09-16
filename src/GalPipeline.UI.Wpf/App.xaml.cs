using System.IO;
using System.Text;
using System.Windows;
using System.Windows.Threading;

namespace GalPipeline.Desktop;

/// <summary>
/// 应用入口：装配全局异常黑匣子（Dispatcher / AppDomain / Task 三路捕获），
/// 崩溃细节完整落盘 gal-pipeline-crash.log，供无 GUI 场景定位根因。
/// </summary>
public partial class App : Application
{
    public App()
    {
        DispatcherUnhandledException += OnDispatcherUnhandledException;
        AppDomain.CurrentDomain.UnhandledException += OnDomainUnhandledException;
        TaskScheduler.UnobservedTaskException += OnUnobservedTaskException;
    }

    private void OnDispatcherUnhandledException(object sender, DispatcherUnhandledExceptionEventArgs e)
    {
        WriteCrash(e.Exception, "DispatcherUnhandledException");
        e.Handled = true; // 记录后保持进程存活，禁止直接闪退
    }

    private void OnDomainUnhandledException(object sender, UnhandledExceptionEventArgs e)
    {
        if (e.ExceptionObject is Exception ex)
        {
            WriteCrash(ex, $"AppDomain.UnhandledException (IsTerminating={e.IsTerminating})");
        }
    }

    private void OnUnobservedTaskException(object? sender, UnobservedTaskExceptionEventArgs e)
    {
        WriteCrash(e.Exception, "UnobservedTaskException");
        e.SetObserved();
    }

    /// <summary>崩溃链完整落盘：外层 + 全部 InnerException 逐层展开。</summary>
    private static void WriteCrash(Exception ex, string source)
    {
        try
        {
            var builder = new StringBuilder();
            builder.AppendLine($"==== {DateTime.Now:yyyy-MM-dd HH:mm:ss.fff} [{source}] ====");
            var current = (Exception?)ex;
            var depth = 0;
            while (current is not null)
            {
                builder.AppendLine($"--- Level {depth} ({current.GetType().FullName}) ---");
                builder.AppendLine(current.ToString());
                current = current.InnerException;
                depth++;
            }
            File.AppendAllText("gal-pipeline-crash.log", builder.ToString(), Encoding.UTF8);
        }
        catch
        {
            // 黑匣子自身异常不再外抛，保障进程稳定
        }
    }
}
