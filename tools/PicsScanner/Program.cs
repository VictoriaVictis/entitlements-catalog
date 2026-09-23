using System.Text.Json;
using SteamKit2;

if (args.Length != 3 || !uint.TryParse(args[0], out var start) ||
    !uint.TryParse(args[1], out var count) || start == 0 || count == 0 ||
    (ulong)start + count > (ulong)uint.MaxValue + 1) {
    Console.Error.WriteLine("Usage: PicsScanner <start-app-id> <count> <output-json>");
    return 2;
}

var client = new SteamClient();
var manager = new CallbackManager(client);
var user = client.GetHandler<SteamUser>()!;
var apps = client.GetHandler<SteamApps>()!;
var completion = new TaskCompletionSource<List<Dlc>>(TaskCreationOptions.RunContinuationsAsynchronously);

manager.Subscribe<SteamClient.ConnectedCallback>(_ => user.LogOnAnonymous());
manager.Subscribe<SteamClient.DisconnectedCallback>(_ =>
    completion.TrySetException(new IOException("Steam disconnected")));
manager.Subscribe<SteamUser.LoggedOnCallback>(callback => {
    if (callback.Result != EResult.OK) {
        completion.TrySetException(new IOException($"Steam login failed: {callback.Result}"));
        return;
    }

    _ = Task.Run(async () => {
        try {
            completion.TrySetResult(await ScanAsync(apps, start, count));
        } catch (Exception error) {
            completion.TrySetException(error);
        }
    });
});

client.Connect();
var deadline = DateTime.UtcNow.AddMinutes(50);
while (!completion.Task.IsCompleted && DateTime.UtcNow < deadline) {
    manager.RunWaitCallbacks(TimeSpan.FromMilliseconds(250));
}

if (!completion.Task.IsCompleted) {
    Console.Error.WriteLine("Steam product-info scan timed out");
    client.Disconnect();
    return 1;
}

try {
    var dlcs = await completion.Task;
    var output = Path.GetFullPath(args[2]);
    Directory.CreateDirectory(Path.GetDirectoryName(output)!);
    var temporary = output + ".tmp";
    var json = JsonSerializer.Serialize(new ScanResult(start, count, dlcs),
        new JsonSerializerOptions { PropertyNamingPolicy = JsonNamingPolicy.CamelCase, WriteIndented = true });
    File.WriteAllText(temporary, json + "\n");
    File.Move(temporary, output, true);
    Console.Error.WriteLine($"Scanned {count} IDs; found {dlcs.Count} DLC records");
    client.Disconnect();
    return 0;
} catch (Exception error) {
    Console.Error.WriteLine($"Steam product-info scan failed: {error.Message}");
    client.Disconnect();
    return 1;
}

static async Task<List<Dlc>> ScanAsync(SteamApps apps, uint start, uint count) {
    var found = new List<Dlc>();
    const uint batchSize = 100;
    for (ulong offset = 0; offset < count; offset += batchSize) {
        var size = (int)Math.Min(batchSize, count - offset);
        var requests = Enumerable.Range(0, size)
            .Select(index => new SteamApps.PICSRequest((uint)(start + offset + (ulong)index)))
            .ToArray();

        Exception? lastError = null;
        for (var attempt = 0; attempt < 3; attempt++) {
            try {
                var response = await apps.PICSGetProductInfo(requests, [])
                    ?? throw new IOException("Steam returned no product-info response");
                foreach (var part in response.Results ?? throw new IOException("Steam returned no product-info results")) {
                    foreach (var (id, info) in part.Apps) {
                        var root = info.KeyValues;
                        if (root is null) continue;
                        var common = root["common"];
                        if (common is null ||
                            !string.Equals(common["type"].Value, "dlc", StringComparison.OrdinalIgnoreCase)) continue;
                        var parentText = common["parent"].Value;
                        if (string.IsNullOrWhiteSpace(parentText)) parentText = root["extended"]["dlcforappid"].Value;
                        if (!uint.TryParse(parentText, out var parent) || parent == 0 || parent == id) continue;
                        var name = common["name"].Value?.Trim();
                        found.Add(new Dlc(id, parent, string.IsNullOrEmpty(name) ? $"DLC {id}" : name));
                    }
                }
                lastError = null;
                break;
            } catch (Exception error) {
                lastError = error;
                if (attempt < 2) await Task.Delay(TimeSpan.FromSeconds(1 << attempt));
            }
        }
        if (lastError is not null) throw new IOException($"Batch starting at {start + offset} failed", lastError);
        if (offset % 10_000 == 0) Console.Error.WriteLine($"Scanned through {start + offset + (ulong)size - 1}");
    }
    return found.OrderBy(item => item.Id).ToList();
}

record Dlc(uint Id, uint Parent, string Name);
record ScanResult(uint Start, uint Count, List<Dlc> Dlcs);
