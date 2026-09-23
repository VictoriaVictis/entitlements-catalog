using System.Text.Json;
using System.Text.RegularExpressions;
using SteamKit2;

Func<SteamApps, Task<object>> work;
string output;
if (args.Length == 3 && args[0] == "--references") {
    var parents = JsonSerializer.Deserialize<List<uint>>(File.ReadAllText(args[1]))
        ?? throw new ArgumentException("Invalid parent-ID input");
    if (parents.Any(id => id == 0)) throw new ArgumentException("Parent IDs must be positive");
    work = async apps => await ReferencesAsync(apps, parents);
    output = args[2];
} else if (args.Length == 3 && uint.TryParse(args[0], out var start) &&
           uint.TryParse(args[1], out var count) && start > 0 && count > 0 &&
           (ulong)start + count <= (ulong)uint.MaxValue + 1) {
    work = async apps => new ScanResult(start, count, await ScanAsync(apps, start, count));
    output = args[2];
} else {
    Console.Error.WriteLine("Usage: PicsScanner <start-app-id> <count> <output-json> | --references <parents-json> <output-json>");
    return 2;
}

var client = new SteamClient();
var manager = new CallbackManager(client);
var user = client.GetHandler<SteamUser>()!;
var apps = client.GetHandler<SteamApps>()!;
var completion = new TaskCompletionSource<object>(TaskCreationOptions.RunContinuationsAsynchronously);

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
            completion.TrySetResult(await work(apps));
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
    Console.Error.WriteLine("Steam product-info request timed out");
    client.Disconnect();
    return 1;
}

try {
    var result = await completion.Task;
    var target = Path.GetFullPath(output);
    Directory.CreateDirectory(Path.GetDirectoryName(target)!);
    var temporary = target + ".tmp";
    var json = JsonSerializer.Serialize(result,
        new JsonSerializerOptions { PropertyNamingPolicy = JsonNamingPolicy.CamelCase, WriteIndented = true });
    File.WriteAllText(temporary, json + "\n");
    File.Move(temporary, target, true);
    Console.Error.WriteLine(result is ScanResult scan
        ? $"Scanned {scan.Count} IDs; found {scan.Dlcs.Count} DLC records"
        : $"Read product-info references for {((ReferenceResult)result).References.Count} games");
    client.Disconnect();
    return 0;
} catch (Exception error) {
    Console.Error.WriteLine($"Steam product-info request failed: {error.Message}");
    client.Disconnect();
    return 1;
}

static async Task<Dictionary<uint, KeyValue>> FetchAsync(SteamApps apps, uint[] ids) {
    Exception? lastError = null;
    for (var attempt = 0; attempt < 3; attempt++) {
        try {
            var requests = ids.Select(id => new SteamApps.PICSRequest(id)).ToArray();
            var response = await apps.PICSGetProductInfo(requests, [])
                ?? throw new IOException("Steam returned no product-info response");
            var result = new Dictionary<uint, KeyValue>();
            foreach (var part in response.Results ?? throw new IOException("Steam returned no product-info results")) {
                foreach (var (id, info) in part.Apps) {
                    if (info.KeyValues is { } root) result[id] = root;
                }
            }
            return result;
        } catch (Exception error) {
            lastError = error;
            if (attempt < 2) await Task.Delay(TimeSpan.FromSeconds(1 << attempt));
        }
    }
    throw new IOException($"Product-info batch starting at {ids[0]} failed", lastError);
}

static async Task<List<Dlc>> ScanAsync(SteamApps apps, uint start, uint count) {
    var found = new List<Dlc>();
    const uint batchSize = 100;
    for (ulong offset = 0; offset < count; offset += batchSize) {
        var size = (int)Math.Min(batchSize, count - offset);
        var ids = Enumerable.Range(0, size).Select(index => (uint)(start + offset + (ulong)index)).ToArray();
        var products = await FetchAsync(apps, ids);
        foreach (var (id, root) in products) {
            var common = root["common"];
            if (common is null ||
                !string.Equals(common["type"].Value, "dlc", StringComparison.OrdinalIgnoreCase)) continue;
            var parentText = common["parent"].Value;
            if (string.IsNullOrWhiteSpace(parentText)) parentText = root["extended"]["dlcforappid"].Value;
            if (!uint.TryParse(parentText, out var parent) || parent == 0 || parent == id) continue;
            var name = common["name"].Value?.Trim();
            found.Add(new Dlc(id, parent, string.IsNullOrEmpty(name) ? $"DLC {id}" : name));
        }
        if (offset % 10_000 == 0) Console.Error.WriteLine($"Scanned through {start + offset + (ulong)size - 1}");
    }
    return found.OrderBy(item => item.Id).ToList();
}

static async Task<ReferenceResult> ReferencesAsync(SteamApps apps, List<uint> parents) {
    var references = new Dictionary<uint, List<uint>>();
    foreach (var batch in parents.Distinct().Order().Chunk(100)) {
        var products = await FetchAsync(apps, batch);
        foreach (var (parent, root) in products) {
            var ids = new HashSet<uint>();
            AddIds(root["extended"]["listofdlc"].Value, ids);
            var depots = root["depots"];
            if (depots is not null) {
                foreach (var depot in depots.Children) AddIds(depot["dlcappid"].Value, ids);
            }
            references[parent] = ids.Order().ToList();
        }
    }
    return new ReferenceResult(references);
}

static void AddIds(string? value, HashSet<uint> ids) {
    if (string.IsNullOrEmpty(value)) return;
    foreach (Match match in Regex.Matches(value, @"\d+")) {
        if (uint.TryParse(match.Value, out var id) && id > 0) ids.Add(id);
    }
}

record Dlc(uint Id, uint Parent, string Name);
record ScanResult(uint Start, uint Count, List<Dlc> Dlcs);
record ReferenceResult(Dictionary<uint, List<uint>> References);
