using System;
using System.Diagnostics;
using System.Text;
using System.Runtime.InteropServices;

public class AskpassBridge {
    [DllImport("kernel32.dll")] static extern IntPtr GetStdHandle(int id);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool WriteFile(IntPtr handle, byte[] data, uint count, out uint written, IntPtr overlapped);
    public static int Main(string[] args) {
        try {
            var info = new ProcessStartInfo();
            info.FileName = Environment.GetEnvironmentVariable("VIBE_SSH_PYTHON");
            info.Arguments = Environment.GetEnvironmentVariable("VIBE_SSH_ARGS") ?? "-I -m vibe_launcher.askpass";
            info.UseShellExecute = false;
            info.CreateNoWindow = true;
            info.RedirectStandardOutput = true;
            info.RedirectStandardError = true;
            info.StandardOutputEncoding = Encoding.UTF8;
            info.EnvironmentVariables["VIBE_SSH_PROMPT"] = String.Join(" ", args);
            using (var process = Process.Start(info)) {
                var output = process.StandardOutput.ReadToEnd();
                process.StandardError.ReadToEnd();
                process.WaitForExit();
                if (process.ExitCode != 0) return 1;
                byte[] bytes = new UTF8Encoding(false).GetBytes(output);
                uint written;
                return WriteFile(GetStdHandle(-11), bytes, (uint)bytes.Length, out written, IntPtr.Zero)
                    && written == bytes.Length ? 0 : 1;
            }
        } catch { return 1; }
    }
}
