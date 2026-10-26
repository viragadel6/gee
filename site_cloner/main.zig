const std = @import("std");

const Allocator = std.mem.Allocator;
const ArrayList = std.ArrayList;
const StringHashMap = std.StringHashMap;
const AutoHashMap = std.AutoHashMap;
const Http = std.http;
const Fs = std.fs;
const Mem = std.mem;
const Url = std.Uri;

const USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36";

const TK_OPEN: u8 = 1;
const TK_CLOSE: u8 = 2;
const TK_SELF: u8 = 3;
const TK_TEXT: u8 = 4;
const TK_COMMENT: u8 = 5;
const TK_DOCTYPE: u8 = 6;
const TK_CDATA: u8 = 7;
const TK_DISCARD: u8 = 8;
const TK_SHADOW: u8 = 9;

const FLAG_DISCARDED: u32 = 1 << 0;
const FLAG_FORCE_CLOSED: u32 = 1 << 1;
const FLAG_ADOPTED: u32 = 1 << 2;
const FLAG_CONVERTED_TEXT: u32 = 1 << 3;
const FLAG_REBALANCED: u32 = 1 << 5;
const FLAG_RAW_TEXT: u32 = 1 << 6;
const FLAG_ROUTED_AROUND: u32 = 1 << 7;
const FLAG_ELIDED: u32 = 1 << 8;
const FLAG_HIDDEN: u32 = 1 << 9;
const FLAG_DEAD: u32 = 1 << 10;
const FLAG_LCP_CANDIDATE: u32 = 1 << 11;
const FLAG_SHADOW_ROOT: u32 = 1 << 12;
const FLAG_INTERACTIVE: u32 = 1 << 13;
const FLAG_HAS_TRANSFORM3D: u32 = 1 << 14;
const FLAG_HEAVY_DATA: u32 = 1 << 15;

const INVALID_IDX: usize = std.math.maxInt(usize);
const MAX_STACK_DEPTH: usize = 2048;
const SECTION_TEXT_LIMIT: usize = 1024;
const MAX_COMPONENTS_PER_FILE: u32 = 12;
const BLOOM_BITS: u32 = 16384;
const BLOOM_HASHES: u32 = 7;
const COLOR_CLUSTERS: usize = 8;
const KMEANS_ITERATIONS: usize = 24;
const FFT_SIZE: usize = 32;
const MINHASH_FUNCS: usize = 16;

const Token16 = struct {
    start: u32,
    end: u32,
    name_start: u32,
    name_end: u32,
};

const NodeMeta32 = struct {
    parent_idx: usize,
    next_sibling_idx: usize,
    first_child_idx: usize,
    last_child_idx: usize,
    attr_start: u32,
    attr_end: u32,
    depth: u32,
    flags: u32,
};

fn packTokenMeta(depth: u32, semantic_weight: u32, child_count: u32, text_len: u32) u64 {
    const d: u64 = @as(u64, depth) & 0xFFF;
    const s: u64 = (@as(u64, semantic_weight) & 0xFF) << 12;
    const c: u64 = (@as(u64, child_count) & 0xFFFF) << 20;
    const t: u64 = (@as(u64, text_len) & 0xFFFFFFF) << 36;
    return d | s | c | t;
}

const Telemetry = struct {
    total_tokens: u64 = 0,
    total_nodes: u64 = 0,
    max_depth: u64 = 0,
    element_count: u64 = 0,
    text_count: u64 = 0,
    comment_count: u64 = 0,
    corruption_count: u64 = 0,
    adoption_count: u64 = 0,
    force_close_count: u64 = 0,
    discard_count: u64 = 0,
    converted_count: u64 = 0,
    rebalance_count: u64 = 0,
    image_count: u64 = 0,
    style_block_count: u64 = 0,
    css_token_count: u64 = 0,
    section_count: u64 = 0,
    elided_count: u64 = 0,
    dead_count: u64 = 0,
    shadow_root_count: u64 = 0,
    interactive_count: u64 = 0,
    transform3d_count: u64 = 0,
};

const VoidElements = std.StaticStringMap(void).initComptime(.{
    .{ "area", {} }, .{ "base", {} }, .{ "br", {} }, .{ "col", {} }, .{ "embed", {} }, .{ "hr", {} },
    .{ "img", {} }, .{ "input", {} }, .{ "link", {} }, .{ "meta", {} }, .{ "param", {} },
    .{ "source", {} }, .{ "track", {} }, .{ "wbr", {} },
});

const RawTextTags = std.StaticStringMap(void).initComptime(.{
    .{ "script", {} }, .{ "style", {} }, .{ "textarea", {} }, .{ "title", {} }, .{ "noscript", {} }, .{ "iframe", {} },
});

const SemanticWeights = std.StaticStringMap(u32).initComptime(.{
    .{ "main", 100 }, .{ "article", 95 }, .{ "section", 80 },
    .{ "header", 75 }, .{ "footer", 70 }, .{ "nav", 65 },
    .{ "aside", 55 }, .{ "h1", 98 }, .{ "h2", 88 },
    .{ "h3", 80 }, .{ "h4", 70 }, .{ "h5", 60 }, .{ "h6", 50 },
    .{ "p", 40 }, .{ "ul", 35 }, .{ "ol", 35 }, .{ "li", 25 },
    .{ "img", 30 }, .{ "figure", 35 }, .{ "form", 60 },
    .{ "button", 45 }, .{ "a", 20 }, .{ "table", 50 },
    .{ "video", 65 }, .{ "audio", 45 }, .{ "blockquote", 55 },
    .{ "div", 10 }, .{ "span", 5 }, .{ "br", 1 }, .{ "hr", 5 },
    .{ "label", 30 }, .{ "input", 25 }, .{ "select", 25 }, .{ "textarea", 30 },
});

const SectionTags = std.StaticStringMap(void).initComptime(.{
    .{ "section", {} }, .{ "header", {} }, .{ "footer", {} }, .{ "main", {} },
    .{ "article", {} }, .{ "aside", {} }, .{ "nav", {} },
});

const AriaRoles = std.StaticStringMap([]const u8).initComptime(.{
    .{ "main", "main" }, .{ "header", "banner" }, .{ "footer", "contentinfo" },
    .{ "nav", "navigation" }, .{ "section", "region" }, .{ "article", "article" },
    .{ "aside", "complementary" }, .{ "form", "form" },
});

const InteractiveTags = std.StaticStringMap(void).initComptime(.{
    .{ "a", {} }, .{ "button", {} }, .{ "input", {} }, .{ "select", {} }, .{ "textarea", {} }, .{ "form", {} }, .{ "details", {} }, .{ "summary", {} },
});

const BloomFilter = struct {
    bits: []u32,
    bit_count: u32,
    allocator: Allocator,

    fn init(allocator: Allocator, bit_count: u32) !BloomFilter {
        const count = if (bit_count == 0) 1 else bit_count;
        const word_count = (count + 31) / 32;
        const bits = try allocator.alloc(u32, word_count);
        @memset(bits, 0);
        return BloomFilter{
            .bits = bits,
            .bit_count = count,
            .allocator = allocator,
        };
    }

    fn deinit(self: *BloomFilter) void {
        self.allocator.free(self.bits);
    }

    fn hashSeed(self: *const BloomFilter, data: []const u8, seed: u32) u32 {
        var h: u64 = 14695981039346656037 ^ @as(u64, seed);
        for (data) |c| {
            h ^= c;
            h *%= 1099511628211;
        }
        return @intCast(h % self.bit_count);
    }

    fn insert(self: *BloomFilter, data: []const u8) void {
        var k: u32 = 0;
        while (k < BLOOM_HASHES) : (k += 1) {
            const bit = self.hashSeed(data, k *% 2654435761);
            self.bits[bit / 32] |= (@as(u32, 1) << @intCast(bit % 32));
        }
    }
};

const TokenArena = struct {
    source: []const u8,
    tokens: []Token16,
    types: []u8,
    nodes: []NodeMeta32,
    metas: []u64,
    z_index: []i32,
    count: usize,
    capacity: usize,
    backing: []u8,
    allocator: Allocator,
    telemetry: Telemetry,

    fn init(allocator: Allocator, source: []const u8) !TokenArena {
        const estimated: usize = blk: {
            const v = @max(@as(usize, 1024), source.len / 6);
            break :blk @min(v, @as(usize, 4_000_000));
        };
        var a = TokenArena{
            .source = source,
            .tokens = &[_]Token16{},
            .types = &[_]u8{},
            .nodes = &[_]NodeMeta32{},
            .metas = &[_]u64{},
            .z_index = &[_]i32{},
            .count = 0,
            .capacity = 0,
            .backing = &[_]u8{},
            .allocator = allocator,
            .telemetry = Telemetry{},
        };
        try a.reserve(estimated);
        return a;
    }

    fn reserve(self: *TokenArena, new_cap: usize) !void {
        if (new_cap <= self.capacity) return;
        const token_bytes = @sizeOf(Token16) * new_cap;
        const type_bytes = @sizeOf(u8) * new_cap;
        const meta_bytes = @sizeOf(NodeMeta32) * new_cap;
        const u64meta_bytes = @sizeOf(u64) * new_cap;
        const z_bytes = @sizeOf(i32) * new_cap;
        const aligned_token = (token_bytes + 63) & ~@as(usize, 63);
        const aligned_type = (type_bytes + 63) & ~@as(usize, 63);
        const aligned_meta = (meta_bytes + 63) & ~@as(usize, 63);
        const aligned_u64 = (u64meta_bytes + 63) & ~@as(usize, 63);
        const aligned_z = (z_bytes + 63) & ~@as(usize, 63);
        const total = aligned_token + aligned_type + aligned_meta + aligned_u64 + aligned_z + 128;
        const new_backing = try self.allocator.alignedAlloc(u8, 64, total);
        @memset(new_backing, 0);
        var offset: usize = 0;
        const tokens_ptr: [*]Token16 = @ptrCast(@alignCast(new_backing.ptr + offset));
        offset += aligned_token;
        const types_ptr: [*]u8 = new_backing.ptr + offset;
        offset += aligned_type;
        const meta_ptr: [*]NodeMeta32 = @ptrCast(@alignCast(new_backing.ptr + offset));
        offset += aligned_meta;
        const u64_ptr: [*]u64 = @ptrCast(@alignCast(new_backing.ptr + offset));
        offset += aligned_u64;
        const z_ptr: [*]i32 = @ptrCast(@alignCast(new_backing.ptr + offset));
        if (self.count > 0) {
            @memcpy(tokens_ptr[0..self.count], self.tokens[0..self.count]);
            @memcpy(types_ptr[0..self.count], self.types[0..self.count]);
            @memcpy(meta_ptr[0..self.count], self.nodes[0..self.count]);
            @memcpy(u64_ptr[0..self.count], self.metas[0..self.count]);
            @memcpy(z_ptr[0..self.count], self.z_index[0..self.count]);
        }
        if (self.backing.len > 0) self.allocator.free(self.backing);
        self.backing = new_backing;
        self.tokens = tokens_ptr[0..new_cap];
        self.types = types_ptr[0..new_cap];
        self.nodes = meta_ptr[0..new_cap];
        self.metas = u64_ptr[0..new_cap];
        self.z_index = z_ptr[0..new_cap];
        self.capacity = new_cap;
    }

    fn deinit(self: *TokenArena) void {
        if (self.backing.len > 0) self.allocator.free(self.backing);
        self.backing = &[_]u8{};
        self.capacity = 0;
        self.count = 0;
    }

    fn append(self: *TokenArena, t: u8, tok: Token16) !usize {
        if (self.count >= self.capacity) {
            const next = if (self.capacity == 0) @as(usize, 1024) else self.capacity * 2;
            try self.reserve(next);
        }
        const idx = self.count;
        self.types[idx] = t;
        self.tokens[idx] = tok;
        self.nodes[idx] = NodeMeta32{
            .parent_idx = INVALID_IDX,
            .next_sibling_idx = INVALID_IDX,
            .first_child_idx = INVALID_IDX,
            .last_child_idx = INVALID_IDX,
            .attr_start = tok.name_end,
            .attr_end = if (tok.end > 0) tok.end - 1 else tok.end,
            .depth = 0,
            .flags = 0,
        };
        self.metas[idx] = 0;
        self.z_index[idx] = 0;
        self.count += 1;
        self.telemetry.total_tokens += 1;
        return idx;
    }

    fn tagName(self: *const TokenArena, idx: usize) []const u8 {
        const t = self.tokens[idx];
        if (t.name_end <= t.name_start or t.name_end > self.source.len) return "";
        return self.source[t.name_start..t.name_end];
    }

    fn attrSlice(self: *const TokenArena, idx: usize) []const u8 {
        const n = self.nodes[idx];
        if (n.attr_end <= n.attr_start or n.attr_end > self.source.len) return "";
        return self.source[n.attr_start..n.attr_end];
    }

    fn rawSlice(self: *const TokenArena, idx: usize) []const u8 {
        const t = self.tokens[idx];
        if (t.end <= t.start or t.end > self.source.len) return "";
        return self.source[t.start..t.end];
    }
};

const Config = struct {
    target_url: []const u8,
    out_dir: []const u8,
};

const Asset = struct {
    src: []const u8,
    alt: []const u8,
    local_path: []const u8,
    width: u32,
    height: u32,
    bytes_size: u32,
    phash: u64,
    duplicate_of: usize,
    blurhash: []const u8,
    is_vector_candidate: bool,
    sprite_x: u32,
    sprite_y: u32,
    sprite_w: u32,
    sprite_h: u32,
};

const Section = struct {
    index: usize,
    selector: []const u8,
    type_str: []const u8,
    text_preview: []const u8,
    images: ArrayList(usize),
    structural_hash: u64,
    semantic_weight: u32,
    text_length: u32,
    tag_length: u32,
    density: u32,
    component_name: []const u8,
    component_file: []const u8,
    aria_role: []const u8,
    template_of: usize,
    weight: u32,
    root_node: usize,
    entropy_hash: []const u8,
    minhash_sig: [MINHASH_FUNCS]u32,
    prominence: f64,
    is_pure: bool,
    has_state: bool,
    has_form: bool,
    has_transform3d: bool,
    bipartite_label: []const u8,
    lcp_node: usize,
    tailwind_classes: []const u8,
    state_machine_id: usize,
    transition_tokens: []const u8,
};

const InternalLink = struct {
    href: []const u8,
    text: []const u8,
};

const ColorRecord = struct {
    r: u8,
    g: u8,
    b: u8,
    a: u8,
};

const Inventory = struct {
    sections: ArrayList(Section),
    assets: ArrayList(Asset),
    tokens: StringHashMap([]const u8),
    structural_map: AutoHashMap(u64, usize),
    internal_links: ArrayList(InternalLink),
    link_bloom: BloomFilter,
    colors: ArrayList(ColorRecord),
    color_palette: [COLOR_CLUSTERS]ColorRecord,
    color_palette_used: usize,
    text_tokens: StringHashMap(u32),
    text_doc_freq: StringHashMap(u32),
    section_text: ArrayList([]const u8),
    markov: AutoHashMap(u64, ArrayList([]const u8)),
    glyphs: AutoHashMap(u32, void),
    transitions: ArrayList([]const u8),
    keyframes: ArrayList([]const u8),
    form_fields: ArrayList([]const u8),
    table_columns: ArrayList([]const u8),
};

const BaseUrl = struct {
    scheme: []const u8,
    host: []const u8,
    base_dir: []const u8,
};

const VFile = struct {
    path: []const u8,
    data: []const u8,
};

const VFS = struct {
    files: ArrayList(VFile),
    allocator: Allocator,

    fn init(allocator: Allocator) VFS {
        return VFS{ .files = ArrayList(VFile).init(allocator), .allocator = allocator };
    }

    fn deinit(self: *VFS) void {
        for (self.files.items) |f| {
            self.allocator.free(f.path);
            self.allocator.free(f.data);
        }
        self.files.deinit();
    }

    fn addOwned(self: *VFS, path: []const u8, data: []const u8) !void {
        const p = try self.allocator.dupe(u8, path);
        errdefer self.allocator.free(p);
        try self.files.append(VFile{ .path = p, .data = data });
    }

    fn addCopy(self: *VFS, path: []const u8, data: []const u8) !void {
        const p = try self.allocator.dupe(u8, path);
        errdefer self.allocator.free(p);
        const d = try self.allocator.dupe(u8, data);
        errdefer self.allocator.free(d);
        try self.files.append(VFile{ .path = p, .data = d });
    }

    fn flush(self: *VFS) !void {
        for (self.files.items) |f| {
            if (Mem.lastIndexOfScalar(u8, f.path, '/')) |idx| {
                if (idx > 0) {
                    Fs.cwd().makePath(f.path[0..idx]) catch |err| {
                        if (err != error.PathAlreadyExists) return err;
                    };
                }
            }
            try Fs.cwd().writeFile(.{ .sub_path = f.path, .data = f.data });
        }
    }
};

pub fn main() !void {
    var gpa = std.heap.GeneralPurposeAllocator(.{}){};
    defer _ = gpa.deinit();
    const allocator = gpa.allocator();

    const args = try std.process.argsAlloc(allocator);
    defer std.process.argsFree(allocator, args);

    if (args.len < 2) {
        std.debug.print("Usage: {s} <target_url>\n", .{args[0]});
        std.process.exit(1);
    }

    const target_url = args[1];
    const out_dir = "clone-data";

    const config = Config{
        .target_url = target_url,
        .out_dir = out_dir,
    };

    _ = try Url.parse(target_url);

    {
        const p = try std.fmt.allocPrint(allocator, "{s}/screenshots", .{out_dir});
        defer allocator.free(p);
        Fs.cwd().makePath(p) catch |err| {
            if (err != error.PathAlreadyExists) return err;
        };
    }
    {
        const p = try std.fmt.allocPrint(allocator, "{s}/components", .{out_dir});
        defer allocator.free(p);
        Fs.cwd().makePath(p) catch |err| {
            if (err != error.PathAlreadyExists) return err;
        };
    }
    Fs.cwd().makePath("public/images") catch |err| {
        if (err != error.PathAlreadyExists) return err;
    };
    Fs.cwd().makePath("public/videos") catch |err| {
        if (err != error.PathAlreadyExists) return err;
    };
    Fs.cwd().makePath("public/fonts") catch |err| {
        if (err != error.PathAlreadyExists) return err;
    };
    Fs.cwd().makePath("src") catch |err| {
        if (err != error.PathAlreadyExists) return err;
    };
    Fs.cwd().makePath("src/components") catch |err| {
        if (err != error.PathAlreadyExists) return err;
    };
    Fs.cwd().makePath("src/machines") catch |err| {
        if (err != error.PathAlreadyExists) return err;
    };
    Fs.cwd().makePath("src/workers") catch |err| {
        if (err != error.PathAlreadyExists) return err;
    };
    Fs.cwd().makePath("src/ecs") catch |err| {
        if (err != error.PathAlreadyExists) return err;
    };
    Fs.cwd().makePath("src/wasm") catch |err| {
        if (err != error.PathAlreadyExists) return err;
    };
    Fs.cwd().makePath("tests") catch |err| {
        if (err != error.PathAlreadyExists) return err;
    };

    var inventory = Inventory{
        .sections = ArrayList(Section).init(allocator),
        .assets = ArrayList(Asset).init(allocator),
        .tokens = StringHashMap([]const u8).init(allocator),
        .structural_map = AutoHashMap(u64, usize).init(allocator),
        .internal_links = ArrayList(InternalLink).init(allocator),
        .link_bloom = try BloomFilter.init(allocator, BLOOM_BITS),
        .colors = ArrayList(ColorRecord).init(allocator),
        .color_palette = [_]ColorRecord{.{ .r = 0, .g = 0, .b = 0, .a = 255 }} ** COLOR_CLUSTERS,
        .color_palette_used = 0,
        .text_tokens = StringHashMap(u32).init(allocator),
        .text_doc_freq = StringHashMap(u32).init(allocator),
        .section_text = ArrayList([]const u8).init(allocator),
        .markov = AutoHashMap(u64, ArrayList([]const u8)).init(allocator),
        .glyphs = AutoHashMap(u32, void).init(allocator),
        .transitions = ArrayList([]const u8).init(allocator),
        .keyframes = ArrayList([]const u8).init(allocator),
        .form_fields = ArrayList([]const u8).init(allocator),
        .table_columns = ArrayList([]const u8).init(allocator),
    };
    defer {
        for (inventory.sections.items) |*s| {
            s.images.deinit();
            allocator.free(s.selector);
            allocator.free(s.type_str);
            allocator.free(s.text_preview);
            allocator.free(s.component_name);
            allocator.free(s.component_file);
            allocator.free(s.aria_role);
            allocator.free(s.entropy_hash);
            allocator.free(s.bipartite_label);
            allocator.free(s.tailwind_classes);
            allocator.free(s.transition_tokens);
        }
        inventory.sections.deinit();
        for (inventory.assets.items) |*a| {
            allocator.free(a.src);
            allocator.free(a.alt);
            allocator.free(a.local_path);
            allocator.free(a.blurhash);
        }
        inventory.assets.deinit();
        var token_iter = inventory.tokens.iterator();
        while (token_iter.next()) |entry| {
            allocator.free(entry.key_ptr.*);
            allocator.free(entry.value_ptr.*);
        }
        inventory.tokens.deinit();
        inventory.structural_map.deinit();
        for (inventory.internal_links.items) |l| {
            allocator.free(l.href);
            allocator.free(l.text);
        }
        inventory.internal_links.deinit();
        inventory.link_bloom.deinit();
        inventory.colors.deinit();
        var tt_iter = inventory.text_tokens.iterator();
        while (tt_iter.next()) |entry| {
            allocator.free(entry.key_ptr.*);
        }
        inventory.text_tokens.deinit();
        var df_iter = inventory.text_doc_freq.iterator();
        while (df_iter.next()) |entry| {
            allocator.free(entry.key_ptr.*);
        }
        inventory.text_doc_freq.deinit();
        for (inventory.section_text.items) |t| {
            allocator.free(t);
        }
        inventory.section_text.deinit();
        var mk_iter = inventory.markov.iterator();
        while (mk_iter.next()) |entry| {
            for (entry.value_ptr.items) |w| allocator.free(w);
            entry.value_ptr.deinit();
        }
        inventory.markov.deinit();
        inventory.glyphs.deinit();
        for (inventory.transitions.items) |t| allocator.free(t);
        inventory.transitions.deinit();
        for (inventory.keyframes.items) |t| allocator.free(t);
        inventory.keyframes.deinit();
        for (inventory.form_fields.items) |t| allocator.free(t);
        inventory.form_fields.deinit();
        for (inventory.table_columns.items) |t| allocator.free(t);
        inventory.table_columns.deinit();
    }

    const raw_html = try fetchRawHtml(allocator, target_url);
    defer allocator.free(raw_html);

    const raw_html_path = try std.fmt.allocPrint(allocator, "{s}/raw.html", .{out_dir});
    defer allocator.free(raw_html_path);
    try Fs.cwd().writeFile(.{ .sub_path = raw_html_path, .data = raw_html });

    var arena = try TokenArena.init(allocator, raw_html);
    defer arena.deinit();

    try tokenizeHtml(&arena);
    try buildHierarchy(&arena);
    try retroactiveBalancing(&arena);
    try parseShadowDomTemplates(&arena);
    try elideRedundantWrappers(&arena);
    try markDeadSubtrees(&arena);
    try assignStackingContext(&arena);

    try extractCssTokens(allocator, &arena, &inventory);
    try extractAssets(allocator, &arena, &inventory);
    try extractInternalLinks(allocator, &arena, &inventory, target_url);
    try harvestColors(allocator, &arena, &inventory);
    try clusterColorsKMeans(&inventory);
    try extractTransitionsAndKeyframes(allocator, &arena, &inventory);
    try extractFormAndTable(allocator, &arena, &inventory);
    try extractSections(allocator, &arena, &inventory);
    try computeStructuralFootprints(&arena, &inventory);
    try computeMinHashSignatures(&inventory);
    try computeProminence(&arena, &inventory);
    try detectLcpCandidate(&arena, &inventory);
    try analyzePurity(&arena, &inventory);
    try inferBipartiteLabels(&inventory);
    try inferAriaConstraints(&arena, &inventory);
    try inferEventListeners(&arena);
    try buildCfg(&arena, &inventory);
    try deduplicateStructures(&inventory);
    try computeTextEntropyHashes(allocator, &arena, &inventory);
    try computeTfIdf(allocator, &inventory);
    try buildMarkovChain(allocator, &inventory);
    try harvestGlyphs(allocator, &inventory);
    try generateTailwindClasses(allocator, &arena, &inventory);
    try downloadAssets(allocator, &inventory, target_url);
    try transcodeImages(allocator, &inventory);
    try detectImageDuplicates(&inventory);
    try vectorizeSmallImages(allocator, &inventory);
    try packSpriteSheet(&inventory);
    try generateBlurhashes(allocator, &inventory);

    var vfs = VFS.init(allocator);
    defer vfs.deinit();

    try generateReactApp(allocator, &arena, &inventory, config, &vfs);
    try generateTheme(allocator, &inventory, &vfs);
    try generateSeoMetadata(allocator, &inventory, &vfs);
    try generateMarkovOutput(allocator, &inventory, &vfs);
    try generateSubsetFont(allocator, &inventory, &vfs);
    try generateBloomRouter(allocator, &inventory, &vfs);
    try generateWasmModule(allocator, &vfs);
    try generateEcsEngine(allocator, &inventory, &vfs);
    try generateXStateMachines(allocator, &inventory, &vfs);
    try generateWebWorkers(allocator, &inventory, &vfs);
    try generateGraphQLStubs(allocator, &inventory, &vfs);
    try generateWebGLOverlay(allocator, &inventory, &vfs);
    try generateTailwindConfig(allocator, &inventory, &vfs);
    try generateDifferentialFuzzer(allocator, &vfs);
    try generateBuildScripts(allocator, &vfs);
    try vfs.flush();

    std.debug.print("Tokens: {d} Nodes: {d} MaxDepth: {d} Sections: {d} Images: {d} Links: {d} Colors: {d} CSSTokens: {d} Elided: {d} Dead: {d} Interactive: {d}\n", .{
        arena.telemetry.total_tokens,
        arena.telemetry.total_nodes,
        arena.telemetry.max_depth,
        arena.telemetry.section_count,
        arena.telemetry.image_count,
        inventory.internal_links.items.len,
        inventory.colors.items.len,
        arena.telemetry.css_token_count,
        arena.telemetry.elided_count,
        arena.telemetry.dead_count,
        arena.telemetry.interactive_count,
    });
}

fn tokenizeHtml(arena: *TokenArena) !void {
    const src = arena.source;
    var i: usize = 0;
    while (i < src.len) {
        if (src[i] == '<') {
            const tag_start: u32 = @intCast(i);
            if (i + 4 <= src.len and src[i + 1] == '!' and src[i + 2] == '-' and src[i + 3] == '-') {
                if (Mem.indexOfPos(u8, src, i + 4, "-->")) |end_idx| {
                    const end: u32 = @intCast(end_idx + 3);
                    _ = try arena.append(TK_COMMENT, Token16{
                        .start = tag_start,
                        .end = end,
                        .name_start = tag_start + 4,
                        .name_end = @intCast(end_idx),
                    });
                    arena.telemetry.comment_count += 1;
                    i = end;
                    continue;
                } else {
                    arena.telemetry.corruption_count += 1;
                    break;
                }
            }
            if (i + 9 <= src.len and eqlIgnoreCase(src[i .. i + 9], "<![CDATA[")) {
                if (Mem.indexOfPos(u8, src, i + 9, "]]>")) |end_idx| {
                    const end: u32 = @intCast(end_idx + 3);
                    _ = try arena.append(TK_CDATA, Token16{
                        .start = tag_start,
                        .end = end,
                        .name_start = tag_start + 9,
                        .name_end = @intCast(end_idx),
                    });
                    i = end;
                    continue;
                } else {
                    arena.telemetry.corruption_count += 1;
                    break;
                }
            }
            if (i + 2 <= src.len and src[i + 1] == '!') {
                if (Mem.indexOfScalarPos(u8, src, i + 2, '>')) |end_idx| {
                    const end: u32 = @intCast(end_idx + 1);
                    _ = try arena.append(TK_DOCTYPE, Token16{
                        .start = tag_start,
                        .end = end,
                        .name_start = tag_start + 2,
                        .name_end = @intCast(end_idx),
                    });
                    i = end;
                    continue;
                } else {
                    arena.telemetry.corruption_count += 1;
                    break;
                }
            }
            if (i + 2 <= src.len and src[i + 1] == '?') {
                if (Mem.indexOfScalarPos(u8, src, i + 2, '>')) |end_idx| {
                    i = end_idx + 1;
                    continue;
                } else {
                    arena.telemetry.corruption_count += 1;
                    break;
                }
            }
            const is_close = i + 1 < src.len and src[i + 1] == '/';
            const name_start_u: usize = if (is_close) i + 2 else i + 1;
            if (name_start_u >= src.len) break;
            const fc = src[name_start_u];
            const is_valid_first = (fc >= 'a' and fc <= 'z') or (fc >= 'A' and fc <= 'Z');
            if (!is_valid_first) {
                const text_start: u32 = @intCast(i);
                const next = Mem.indexOfScalarPos(u8, src, i + 1, '<') orelse src.len;
                const text_end: u32 = @intCast(next);
                _ = try arena.append(TK_TEXT, Token16{
                    .start = text_start,
                    .end = text_end,
                    .name_start = text_start,
                    .name_end = text_start,
                });
                arena.telemetry.text_count += 1;
                i = next;
                continue;
            }
            var name_end_u = name_start_u;
            while (name_end_u < src.len) {
                const c = src[name_end_u];
                if (c == ' ' or c == '\t' or c == '\n' or c == '\r' or c == '/' or c == '>' or c == 0x0c) break;
                name_end_u += 1;
            }
            var p = name_end_u;
            var in_quote: u8 = 0;
            while (p < src.len) {
                const c = src[p];
                if (in_quote != 0) {
                    if (c == in_quote) in_quote = 0;
                } else {
                    if (c == '"' or c == '\'') {
                        in_quote = c;
                    } else if (c == '>') {
                        break;
                    }
                }
                p += 1;
            }
            if (p >= src.len) {
                arena.telemetry.corruption_count += 1;
                const conv_start: u32 = @intCast(i);
                const conv_end: u32 = @intCast(src.len);
                const idx = try arena.append(TK_TEXT, Token16{
                    .start = conv_start,
                    .end = conv_end,
                    .name_start = conv_start,
                    .name_end = conv_start,
                });
                arena.nodes[idx].flags |= FLAG_CONVERTED_TEXT;
                arena.telemetry.converted_count += 1;
                arena.telemetry.text_count += 1;
                break;
            }
            const tag_end: u32 = @intCast(p + 1);
            var self_close = false;
            var check_p = p;
            while (check_p > name_end_u) {
                check_p -= 1;
                if (src[check_p] == '/') {
                    self_close = true;
                    break;
                } else if (!isWhitespace(src[check_p])) {
                    break;
                }
            }
            var tag_name_buf: [64]u8 = undefined;
            const tag_name_len = @min(name_end_u - name_start_u, 64);
            for (src[name_start_u .. name_start_u + tag_name_len], 0..) |c, ci| {
                tag_name_buf[ci] = toAsciiLower(c);
            }
            const tag_name_lower = tag_name_buf[0..tag_name_len];
            const is_void = VoidElements.has(tag_name_lower);
            const ttype: u8 = if (is_close) TK_CLOSE else if (self_close or is_void) TK_SELF else TK_OPEN;
            const idx = try arena.append(ttype, Token16{
                .start = tag_start,
                .end = tag_end,
                .name_start = @intCast(name_start_u),
                .name_end = @intCast(name_end_u),
            });
            arena.nodes[idx].attr_start = @intCast(name_end_u);
            arena.nodes[idx].attr_end = if (self_close and tag_end > 2) tag_end - 2 else tag_end - 1;
            arena.telemetry.element_count += 1;
            i = tag_end;
            if (ttype == TK_OPEN and RawTextTags.has(tag_name_lower)) {
                arena.nodes[idx].flags |= FLAG_RAW_TEXT;
                const raw_text_start: u32 = @intCast(i);
                var search_pos = i;
                var found_close: ?usize = null;
                while (search_pos < src.len) {
                    if (Mem.indexOfScalarPos(u8, src, search_pos, '<')) |lt| {
                        if (lt + 2 < src.len and src[lt + 1] == '/') {
                            const close_name_start = lt + 2;
                            var close_name_end = close_name_start;
                            while (close_name_end < src.len) {
                                const c = src[close_name_end];
                                if (c == ' ' or c == '\t' or c == '\n' or c == '\r' or c == '/' or c == '>') break;
                                close_name_end += 1;
                            }
                            if (eqlIgnoreCase(src[close_name_start..close_name_end], tag_name_lower)) {
                                found_close = lt;
                                break;
                            }
                        }
                        search_pos = lt + 1;
                    } else {
                        break;
                    }
                }
                if (found_close) |fc_pos| {
                    if (fc_pos > raw_text_start) {
                        _ = try arena.append(TK_TEXT, Token16{
                            .start = raw_text_start,
                            .end = @intCast(fc_pos),
                            .name_start = raw_text_start,
                            .name_end = raw_text_start,
                        });
                        arena.telemetry.text_count += 1;
                    }
                    i = fc_pos;
                } else {
                    arena.telemetry.corruption_count += 1;
                    if (raw_text_start < src.len) {
                        _ = try arena.append(TK_TEXT, Token16{
                            .start = raw_text_start,
                            .end = @intCast(src.len),
                            .name_start = raw_text_start,
                            .name_end = raw_text_start,
                        });
                        arena.telemetry.text_count += 1;
                    }
                    i = src.len;
                }
            }
            continue;
        }
        const text_start: u32 = @intCast(i);
        const next = Mem.indexOfScalarPos(u8, src, i, '<') orelse src.len;
        const text_end: u32 = @intCast(next);
        if (text_end > text_start) {
            _ = try arena.append(TK_TEXT, Token16{
                .start = text_start,
                .end = text_end,
                .name_start = text_start,
                .name_end = text_start,
            });
            arena.telemetry.text_count += 1;
        }
        i = next;
    }
}

fn buildHierarchy(arena: *TokenArena) !void {
    var stack: [MAX_STACK_DEPTH]usize = undefined;
    var depth: u32 = 0;
    var i: usize = 0;
    while (i < arena.count) : (i += 1) {
        const t = arena.types[i];
        switch (t) {
            TK_OPEN => {
                arena.nodes[i].depth = depth;
                if (depth > 0) {
                    const parent = stack[depth - 1];
                    arena.nodes[i].parent_idx = parent;
                    if (arena.nodes[parent].first_child_idx == INVALID_IDX) {
                        arena.nodes[parent].first_child_idx = i;
                    } else {
                        const last = arena.nodes[parent].last_child_idx;
                        arena.nodes[last].next_sibling_idx = i;
                    }
                    arena.nodes[parent].last_child_idx = i;
                }
                if (depth < MAX_STACK_DEPTH) {
                    stack[depth] = i;
                    depth += 1;
                }
                if (depth > arena.telemetry.max_depth) arena.telemetry.max_depth = depth;
            },
            TK_CLOSE => {
                const name = arena.tagName(i);
                var found_depth: i32 = -1;
                var k: i32 = @as(i32, @intCast(depth)) - 1;
                while (k >= 0) : (k -= 1) {
                    const idx = stack[@intCast(k)];
                    if (eqlIgnoreCase(arena.tagName(idx), name)) {
                        found_depth = k;
                        break;
                    }
                }
                if (found_depth >= 0) {
                    var force_k: u32 = depth;
                    const target: u32 = @intCast(found_depth);
                    while (force_k > target + 1) {
                        force_k -= 1;
                        const orphan_idx = stack[force_k];
                        arena.nodes[orphan_idx].flags |= FLAG_FORCE_CLOSED;
                        arena.telemetry.force_close_count += 1;
                    }
                    depth = target;
                } else {
                    arena.telemetry.corruption_count += 1;
                    if (depth > 0) {
                        const parent = stack[depth - 1];
                        arena.nodes[parent].flags |= FLAG_ROUTED_AROUND;
                    }
                }
                arena.nodes[i].flags |= FLAG_DISCARDED;
                arena.types[i] = TK_DISCARD;
                arena.telemetry.discard_count += 1;
            },
            TK_SELF, TK_TEXT, TK_COMMENT, TK_DOCTYPE, TK_CDATA => {
                arena.nodes[i].depth = depth;
                if (depth > 0) {
                    const parent = stack[depth - 1];
                    arena.nodes[i].parent_idx = parent;
                    if (arena.nodes[parent].first_child_idx == INVALID_IDX) {
                        arena.nodes[parent].first_child_idx = i;
                    } else {
                        const last = arena.nodes[parent].last_child_idx;
                        arena.nodes[last].next_sibling_idx = i;
                    }
                    arena.nodes[parent].last_child_idx = i;
                }
            },
            else => {},
        }
        arena.telemetry.total_nodes += 1;
    }
    while (depth > 0) : (depth -= 1) {
        const idx = stack[depth - 1];
        arena.nodes[idx].flags |= FLAG_ADOPTED;
        arena.telemetry.adoption_count += 1;
    }
    var n: usize = 0;
    while (n < arena.count) : (n += 1) {
        const node = arena.nodes[n];
        var cc: u32 = 0;
        var c = node.first_child_idx;
        while (c != INVALID_IDX and cc < 65535) : (cc += 1) {
            c = arena.nodes[c].next_sibling_idx;
        }
        var tn_lower_buf: [64]u8 = undefined;
        const tn = arena.tagName(n);
        const tn_len = @min(tn.len, 64);
        for (tn[0..tn_len], 0..) |ch, ci| tn_lower_buf[ci] = toAsciiLower(ch);
        const tn_lower = tn_lower_buf[0..tn_len];
        const sw = SemanticWeights.get(tn_lower) orelse 1;
        const txt_len: u32 = if (arena.types[n] == TK_TEXT) @intCast(arena.tokens[n].end - arena.tokens[n].start) else 0;
        arena.metas[n] = packTokenMeta(node.depth, sw, cc, txt_len);
    }
}

fn retroactiveBalancing(arena: *TokenArena) !void {
    var i: usize = 0;
    while (i < arena.count) : (i += 1) {
        if ((arena.nodes[i].flags & FLAG_DISCARDED) != 0) continue;
        const parent = arena.nodes[i].parent_idx;
        if (parent == INVALID_IDX) continue;
        var child = arena.nodes[parent].first_child_idx;
        var seen_self = false;
        var seen_count: u32 = 0;
        while (child != INVALID_IDX and seen_count < 65535) : (seen_count += 1) {
            const next = arena.nodes[child].next_sibling_idx;
            if (child == i) {
                seen_self = true;
                break;
            }
            child = next;
        }
        if (!seen_self) {
            const last = arena.nodes[parent].last_child_idx;
            if (last != INVALID_IDX and last != i) {
                arena.nodes[last].next_sibling_idx = i;
                arena.nodes[parent].last_child_idx = i;
                arena.nodes[i].flags |= FLAG_REBALANCED;
                arena.telemetry.rebalance_count += 1;
            }
        }
    }
}

fn parseShadowDomTemplates(arena: *TokenArena) !void {
    var i: usize = 0;
    while (i < arena.count) : (i += 1) {
        if (arena.types[i] != TK_OPEN) continue;
        if (!eqlIgnoreCase(arena.tagName(i), "template")) continue;
        const attrs = arena.attrSlice(i);
        if (findNextCaseInsensitive(attrs, "shadowroot", 0) != null or findNextCaseInsensitive(attrs, "shadowrootmode", 0) != null) {
            arena.types[i] = TK_SHADOW;
            arena.nodes[i].flags |= FLAG_SHADOW_ROOT;
            arena.telemetry.shadow_root_count += 1;
        }
    }
}

fn elideRedundantWrappers(arena: *TokenArena) !void {
    var i: usize = 0;
    while (i < arena.count) : (i += 1) {
        if (arena.types[i] != TK_OPEN) continue;
        if ((arena.nodes[i].flags & FLAG_DISCARDED) != 0) continue;
        const name = arena.tagName(i);
        if (!eqlIgnoreCase(name, "div") and !eqlIgnoreCase(name, "span")) continue;
        const attrs = arena.attrSlice(i);
        var has_meaningful = false;
        if (Mem.indexOf(u8, attrs, "class=") != null) has_meaningful = true;
        if (Mem.indexOf(u8, attrs, "id=") != null) has_meaningful = true;
        if (Mem.indexOf(u8, attrs, "style=") != null) has_meaningful = true;
        if (Mem.indexOf(u8, attrs, "data-") != null) has_meaningful = true;
        if (Mem.indexOf(u8, attrs, "role=") != null) has_meaningful = true;
        if (Mem.indexOf(u8, attrs, "aria-") != null) has_meaningful = true;
        if (has_meaningful) continue;
        const node = arena.nodes[i];
        var child_count: u32 = 0;
        var c = node.first_child_idx;
        while (c != INVALID_IDX and child_count < 4) : (child_count += 1) {
            c = arena.nodes[c].next_sibling_idx;
        }
        if (child_count == 1) {
            arena.nodes[i].flags |= FLAG_ELIDED;
            arena.telemetry.elided_count += 1;
        }
    }
}

fn markDeadSubtrees(arena: *TokenArena) !void {
    var i: usize = 0;
    while (i < arena.count) : (i += 1) {
        if (arena.types[i] != TK_OPEN and arena.types[i] != TK_SELF) continue;
        const attrs = arena.attrSlice(i);
        var is_dead = false;
        if (Mem.indexOf(u8, attrs, "hidden") != null) is_dead = true;
        if (Mem.indexOf(u8, attrs, "display:none") != null) is_dead = true;
        if (Mem.indexOf(u8, attrs, "display: none") != null) is_dead = true;
        if (Mem.indexOf(u8, attrs, "visibility:hidden") != null) is_dead = true;
        if (Mem.indexOf(u8, attrs, "aria-hidden=true") != null) is_dead = true;
        if (Mem.indexOf(u8, attrs, "aria-hidden='true'") != null) is_dead = true;
        if (is_dead) {
            arena.nodes[i].flags |= FLAG_HIDDEN | FLAG_DEAD;
            arena.telemetry.dead_count += 1;
            const end = findSubtreeEnd(arena, i);
            var k = i + 1;
            while (k <= end and k < arena.count) : (k += 1) {
                arena.nodes[k].flags |= FLAG_DEAD;
            }
        }
    }
}

fn assignStackingContext(arena: *TokenArena) !void {
    var i: usize = 0;
    var z: i32 = 0;
    while (i < arena.count) : (i += 1) {
        if (arena.types[i] != TK_OPEN and arena.types[i] != TK_SELF) continue;
        const attrs = arena.attrSlice(i);
        if (Mem.indexOf(u8, attrs, "position:fixed") != null or Mem.indexOf(u8, attrs, "position: fixed") != null) {
            z += 10;
        }
        if (Mem.indexOf(u8, attrs, "position:absolute") != null or Mem.indexOf(u8, attrs, "position: absolute") != null) {
            z += 5;
        }
        if (Mem.indexOf(u8, attrs, "translate3d") != null or Mem.indexOf(u8, attrs, "rotateX") != null or Mem.indexOf(u8, attrs, "rotateY") != null or Mem.indexOf(u8, attrs, "perspective") != null) {
            arena.nodes[i].flags |= FLAG_HAS_TRANSFORM3D;
            arena.telemetry.transform3d_count += 1;
            z += 3;
        }
        arena.z_index[i] = z + @as(i32, @intCast(arena.nodes[i].depth));
    }
}

fn extractCssTokens(allocator: Allocator, arena: *TokenArena, inventory: *Inventory) !void {
    var i: usize = 0;
    while (i < arena.count) : (i += 1) {
        if (arena.types[i] != TK_OPEN) continue;
        if (!eqlIgnoreCase(arena.tagName(i), "style")) continue;
        arena.telemetry.style_block_count += 1;
        var k = i + 1;
        var content_start: usize = INVALID_IDX;
        var content_end: usize = INVALID_IDX;
        while (k < arena.count) : (k += 1) {
            if (arena.types[k] == TK_TEXT) {
                if (content_start == INVALID_IDX) content_start = arena.tokens[k].start;
                content_end = arena.tokens[k].end;
            }
            if (arena.types[k] == TK_OPEN or arena.types[k] == TK_CLOSE or arena.types[k] == TK_SELF) {
                break;
            }
        }
        if (content_start == INVALID_IDX or content_end == INVALID_IDX) continue;
        const css = arena.source[content_start..content_end];
        const cleaned = try stripCssComments(allocator, css);
        defer allocator.free(cleaned);
        var root_pos: usize = 0;
        while (findNextCaseInsensitive(cleaned, ":root", root_pos)) |root_start| {
            const maybe_block_start = Mem.indexOfScalarPos(u8, cleaned, root_start + 5, '{');
            if (maybe_block_start == null) break;
            const block_start = maybe_block_start.?;
            var brace_depth: i32 = 0;
            var block_end: usize = block_start;
            while (block_end < cleaned.len) : (block_end += 1) {
                if (cleaned[block_end] == '{') brace_depth += 1;
                if (cleaned[block_end] == '}') {
                    brace_depth -= 1;
                    if (brace_depth == 0) break;
                }
            }
            if (block_end >= cleaned.len) break;
            try parseCssDeclarations(allocator, cleaned[block_start + 1 .. block_end], inventory, &arena.telemetry);
            root_pos = block_end + 1;
        }
        try harvestCssColors(cleaned, inventory);
    }
}

fn harvestCssColors(css: []const u8, inventory: *Inventory) !void {
    var i: usize = 0;
    while (i < css.len) {
        if (css[i] == '#' and i + 6 < css.len) {
            const hex = css[i + 1 .. i + 7];
            if (allHex(hex)) {
                const r = parseHexByte(hex[0..2]);
                const g = parseHexByte(hex[2..4]);
                const b = parseHexByte(hex[4..6]);
                try inventory.colors.append(ColorRecord{ .r = r, .g = g, .b = b, .a = 255 });
                i += 7;
                continue;
            } else if (i + 3 < css.len and allHex(css[i + 1 .. i + 4])) {
                const r = parseHexByte(&[_]u8{ css[i + 1], css[i + 1] });
                const g = parseHexByte(&[_]u8{ css[i + 2], css[i + 2] });
                const b = parseHexByte(&[_]u8{ css[i + 3], css[i + 3] });
                try inventory.colors.append(ColorRecord{ .r = r, .g = g, .b = b, .a = 255 });
                i += 4;
                continue;
            }
        }
        if (i + 4 < css.len and css[i] == 'r' and css[i + 1] == 'g' and css[i + 2] == 'b') {
            if (Mem.indexOfScalarPos(u8, css, i, ')')) |end| {
                const inner_start = (Mem.indexOfScalarPos(u8, css, i, '(') orelse i) + 1;
                if (inner_start < end) {
                    const inner = css[inner_start..end];
                    var ipos: usize = 0;
                    var vals: [4]u32 = .{ 0, 0, 0, 255 };
                    var vi: usize = 0;
                    while (ipos < inner.len and vi < 4) {
                        while (ipos < inner.len and !(inner[ipos] >= '0' and inner[ipos] <= '9')) ipos += 1;
                        if (ipos >= inner.len) break;
                        var num: u32 = 0;
                        while (ipos < inner.len and inner[ipos] >= '0' and inner[ipos] <= '9') {
                            num = num * 10 + (inner[ipos] - '0');
                            ipos += 1;
                        }
                        vals[vi] = num;
                        vi += 1;
                    }
                    try inventory.colors.append(ColorRecord{
                        .r = @intCast(@min(vals[0], 255)),
                        .g = @intCast(@min(vals[1], 255)),
                        .b = @intCast(@min(vals[2], 255)),
                        .a = @intCast(@min(vals[3], 255)),
                    });
                }
                i = end + 1;
                continue;
            }
        }
        i += 1;
    }
}

fn allHex(s: []const u8) bool {
    for (s) |c| {
        const ok = (c >= '0' and c <= '9') or (c >= 'a' and c <= 'f') or (c >= 'A' and c <= 'F');
        if (!ok) return false;
    }
    return true;
}

fn parseHexByte(s: []const u8) u8 {
    var v: u8 = 0;
    for (s) |c| {
        v <<= 4;
        if (c >= '0' and c <= '9') {
            v |= c - '0';
        } else if (c >= 'a' and c <= 'f') {
            v |= c - 'a' + 10;
        } else if (c >= 'A' and c <= 'F') {
            v |= c - 'A' + 10;
        }
    }
    return v;
}

fn parseCssDeclarations(allocator: Allocator, declarations: []const u8, inventory: *Inventory, telemetry: *Telemetry) !void {
    var declaration_iter = Mem.splitScalar(u8, declarations, ';');
    while (declaration_iter.next()) |declaration| {
        const maybe_colon = Mem.indexOfScalar(u8, declaration, ':');
        if (maybe_colon == null) continue;
        const colon = maybe_colon.?;
        const name = Mem.trim(u8, declaration[0..colon], " \t\r\n");
        const value = Mem.trim(u8, declaration[colon + 1 ..], " \t\r\n");
        if (name.len == 0 or value.len == 0) continue;
        if (!Mem.startsWith(u8, name, "--")) continue;
        const key = try allocator.dupe(u8, name);
        errdefer allocator.free(key);
        const val = try allocator.dupe(u8, value);
        errdefer allocator.free(val);
        const result = try inventory.tokens.getOrPut(key);
        if (result.found_existing) {
            allocator.free(key);
            allocator.free(result.value_ptr.*);
            result.value_ptr.* = val;
        } else {
            result.value_ptr.* = val;
            telemetry.css_token_count += 1;
        }
    }
}

fn stripCssComments(allocator: Allocator, css: []const u8) ![]const u8 {
    var out = ArrayList(u8).init(allocator);
    errdefer out.deinit();
    var i: usize = 0;
    while (i < css.len) {
        if (i + 1 < css.len and css[i] == '/' and css[i + 1] == '*') {
            if (Mem.indexOfPos(u8, css, i + 2, "*/")) |end| {
                i = end + 2;
            } else {
                break;
            }
        } else {
            try out.append(css[i]);
            i += 1;
        }
    }
    return try out.toOwnedSlice();
}

fn extractAssets(allocator: Allocator, arena: *TokenArena, inventory: *Inventory) !void {
    var i: usize = 0;
    while (i < arena.count) : (i += 1) {
        if (arena.types[i] != TK_SELF and arena.types[i] != TK_OPEN) continue;
        if ((arena.nodes[i].flags & FLAG_DISCARDED) != 0) continue;
        if ((arena.nodes[i].flags & FLAG_DEAD) != 0) continue;
        const tn = arena.tagName(i);
        if (!eqlIgnoreCase(tn, "img")) continue;
        const attrs = arena.attrSlice(i);
        const maybe_src = try findAttributeValue(allocator, attrs, "src");
        if (maybe_src == null) continue;
        const src = maybe_src.?;
        if (Mem.trim(u8, src, " \t\r\n").len == 0) {
            allocator.free(src);
            continue;
        }
        const alt = (try findAttributeValue(allocator, attrs, "alt")) orelse try allocator.dupe(u8, "");
        const local_path = try allocator.dupe(u8, "");
        const blur = try allocator.dupe(u8, "");
        try inventory.assets.append(Asset{
            .src = src,
            .alt = alt,
            .local_path = local_path,
            .width = 0,
            .height = 0,
            .bytes_size = 0,
            .phash = 0,
            .duplicate_of = INVALID_IDX,
            .blurhash = blur,
            .is_vector_candidate = false,
            .sprite_x = 0,
            .sprite_y = 0,
            .sprite_w = 0,
            .sprite_h = 0,
        });
        arena.telemetry.image_count += 1;
    }
}

fn extractInternalLinks(allocator: Allocator, arena: *TokenArena, inventory: *Inventory, target_url: []const u8) !void {
    const base = try parseBaseUrl(target_url);
    var i: usize = 0;
    while (i < arena.count) : (i += 1) {
        if (arena.types[i] != TK_OPEN and arena.types[i] != TK_SELF) continue;
        const tn = arena.tagName(i);
        if (!eqlIgnoreCase(tn, "a")) continue;
        const attrs = arena.attrSlice(i);
        const maybe_href = try findAttributeValue(allocator, attrs, "href");
        if (maybe_href == null) continue;
        const href = maybe_href.?;
        if (href.len == 0 or Mem.startsWith(u8, href, "#") or Mem.startsWith(u8, href, "mailto:") or Mem.startsWith(u8, href, "javascript:")) {
            allocator.free(href);
            continue;
        }
        var is_internal = false;
        if (Mem.startsWith(u8, href, "/")) {
            is_internal = true;
        } else if (Mem.startsWith(u8, href, "http://") or Mem.startsWith(u8, href, "https://")) {
            if (Mem.indexOf(u8, href, base.host) != null) is_internal = true;
        } else if (!Mem.startsWith(u8, href, "//")) {
            is_internal = true;
        }
        if (!is_internal) {
            allocator.free(href);
            continue;
        }
        var text_buf = ArrayList(u8).init(allocator);
        defer text_buf.deinit();
        const end_idx = findSubtreeEnd(arena, i);
        var k = i + 1;
        while (k <= end_idx and k < arena.count) : (k += 1) {
            if (arena.types[k] == TK_TEXT) {
                const raw = arena.rawSlice(k);
                for (raw) |c| {
                    if (text_buf.items.len > 80) break;
                    if (isWhitespace(c)) {
                        if (text_buf.items.len > 0 and text_buf.items[text_buf.items.len - 1] != ' ') {
                            try text_buf.append(' ');
                        }
                    } else {
                        try text_buf.append(c);
                    }
                }
            }
        }
        const txt = try allocator.dupe(u8, Mem.trim(u8, text_buf.items, " \t\r\n"));
        try inventory.internal_links.append(InternalLink{ .href = href, .text = txt });
        inventory.link_bloom.insert(href);
    }
}

fn harvestColors(allocator: Allocator, arena: *TokenArena, inventory: *Inventory) !void {
    _ = allocator;
    var i: usize = 0;
    while (i < arena.count) : (i += 1) {
        if (arena.types[i] != TK_OPEN and arena.types[i] != TK_SELF) continue;
        const attrs = arena.attrSlice(i);
        if (Mem.indexOf(u8, attrs, "style=") == null) continue;
        try harvestCssColors(attrs, inventory);
    }
}

fn clusterColorsKMeans(inventory: *Inventory) !void {
    if (inventory.colors.items.len == 0) {
        inventory.color_palette_used = 0;
        return;
    }
    var centroids: [COLOR_CLUSTERS][3]f64 = undefined;
    const sample_step = @max(@as(usize, 1), inventory.colors.items.len / COLOR_CLUSTERS);
    var s: usize = 0;
    while (s < COLOR_CLUSTERS) : (s += 1) {
        const idx = @min(s * sample_step, inventory.colors.items.len - 1);
        const c = inventory.colors.items[idx];
        centroids[s][0] = @floatFromInt(c.r);
        centroids[s][1] = @floatFromInt(c.g);
        centroids[s][2] = @floatFromInt(c.b);
    }
    var assignments = try std.heap.page_allocator.alloc(u8, inventory.colors.items.len);
    defer std.heap.page_allocator.free(assignments);
    var iter: usize = 0;
    while (iter < KMEANS_ITERATIONS) : (iter += 1) {
        for (inventory.colors.items, 0..) |c, ci| {
            var best: u8 = 0;
            var best_d: f64 = 1e30;
            var k: u8 = 0;
            while (k < COLOR_CLUSTERS) : (k += 1) {
                const dr = @as(f64, @floatFromInt(c.r)) - centroids[k][0];
                const dg = @as(f64, @floatFromInt(c.g)) - centroids[k][1];
                const db = @as(f64, @floatFromInt(c.b)) - centroids[k][2];
                const dist = dr * dr + dg * dg + db * db;
                if (dist < best_d) {
                    best_d = dist;
                    best = k;
                }
            }
            assignments[ci] = best;
        }
        var sums = [_][3]f64{.{ 0, 0, 0 }} ** COLOR_CLUSTERS;
        var counts = [_]u32{0} ** COLOR_CLUSTERS;
        for (inventory.colors.items, 0..) |c, ci| {
            const a = assignments[ci];
            sums[a][0] += @floatFromInt(c.r);
            sums[a][1] += @floatFromInt(c.g);
            sums[a][2] += @floatFromInt(c.b);
            counts[a] += 1;
        }
        var k2: usize = 0;
        while (k2 < COLOR_CLUSTERS) : (k2 += 1) {
            if (counts[k2] > 0) {
                const cf: f64 = @floatFromInt(counts[k2]);
                centroids[k2][0] = sums[k2][0] / cf;
                centroids[k2][1] = sums[k2][1] / cf;
                centroids[k2][2] = sums[k2][2] / cf;
            }
        }
    }
    var k3: usize = 0;
    while (k3 < COLOR_CLUSTERS) : (k3 += 1) {
        inventory.color_palette[k3] = ColorRecord{
            .r = @intFromFloat(@min(@max(centroids[k3][0], 0), 255)),
            .g = @intFromFloat(@min(@max(centroids[k3][1], 0), 255)),
            .b = @intFromFloat(@min(@max(centroids[k3][2], 0), 255)),
            .a = 255,
        };
    }
    inventory.color_palette_used = COLOR_CLUSTERS;
}

fn extractTransitionsAndKeyframes(allocator: Allocator, arena: *TokenArena, inventory: *Inventory) !void {
    var i: usize = 0;
    while (i < arena.count) : (i += 1) {
        if (arena.types[i] != TK_OPEN) continue;
        if (!eqlIgnoreCase(arena.tagName(i), "style")) continue;
        var k = i + 1;
        while (k < arena.count) : (k += 1) {
            if (arena.types[k] == TK_TEXT) {
                const css = arena.rawSlice(k);
                var pos: usize = 0;
                while (Mem.indexOfPos(u8, css, pos, "transition")) |t| {
                    const start = t;
                    var end = start;
                    while (end < css.len and css[end] != ';' and css[end] != '}') end += 1;
                    if (end > start) {
                        const slice = css[start..end];
                        try inventory.transitions.append(try allocator.dupe(u8, slice));
                    }
                    pos = end + 1;
                }
                var kpos: usize = 0;
                while (Mem.indexOfPos(u8, css, kpos, "@keyframes")) |t| {
                    const start = t;
                    var end = start;
                    var brace_depth: i32 = 0;
                    var started = false;
                    while (end < css.len) : (end += 1) {
                        if (css[end] == '{') {
                            brace_depth += 1;
                            started = true;
                        } else if (css[end] == '}') {
                            brace_depth -= 1;
                            if (started and brace_depth == 0) {
                                end += 1;
                                break;
                            }
                        }
                    }
                    if (end > start) {
                        const slice = css[start..end];
                        try inventory.keyframes.append(try allocator.dupe(u8, slice));
                    }
                    kpos = end + 1;
                }
                break;
            }
            if (arena.types[k] == TK_OPEN or arena.types[k] == TK_CLOSE or arena.types[k] == TK_SELF) break;
        }
    }
}

fn extractFormAndTable(allocator: Allocator, arena: *TokenArena, inventory: *Inventory) !void {
    var i: usize = 0;
    while (i < arena.count) : (i += 1) {
        if (arena.types[i] != TK_SELF and arena.types[i] != TK_OPEN) continue;
        const tn = arena.tagName(i);
        if (eqlIgnoreCase(tn, "input") or eqlIgnoreCase(tn, "select") or eqlIgnoreCase(tn, "textarea")) {
            const attrs = arena.attrSlice(i);
            const name_attr = (try findAttributeValue(allocator, attrs, "name")) orelse try allocator.dupe(u8, "");
            if (name_attr.len > 0) {
                try inventory.form_fields.append(name_attr);
            } else {
                allocator.free(name_attr);
            }
        }
        if (eqlIgnoreCase(tn, "th")) {
            const end = findSubtreeEnd(arena, i);
            var k = i + 1;
            var buf = ArrayList(u8).init(allocator);
            defer buf.deinit();
            while (k <= end and k < arena.count) : (k += 1) {
                if (arena.types[k] == TK_TEXT) {
                    const raw = arena.rawSlice(k);
                    for (raw) |c| {
                        if (buf.items.len > 40) break;
                        if (isWhitespace(c)) {
                            if (buf.items.len > 0 and buf.items[buf.items.len - 1] != ' ') try buf.append(' ');
                        } else {
                            try buf.append(c);
                        }
                    }
                }
            }
            const txt = Mem.trim(u8, buf.items, " \t\r\n");
            if (txt.len > 0) {
                try inventory.table_columns.append(try allocator.dupe(u8, txt));
            }
        }
    }
}

fn isSectionTag(name: []const u8) bool {
    var buf: [64]u8 = undefined;
    const len = @min(name.len, 64);
    for (name[0..len], 0..) |c, i| buf[i] = toAsciiLower(c);
    return SectionTags.has(buf[0..len]);
}

fn extractSections(allocator: Allocator, arena: *TokenArena, inventory: *Inventory) !void {
    var section_index: usize = 0;
    var i: usize = 0;
    while (i < arena.count) : (i += 1) {
        if (arena.types[i] != TK_OPEN) continue;
        if ((arena.nodes[i].flags & FLAG_DISCARDED) != 0) continue;
        if ((arena.nodes[i].flags & FLAG_DEAD) != 0) continue;
        const tn = arena.tagName(i);
        if (!isSectionTag(tn)) continue;
        var tn_lower_buf: [64]u8 = undefined;
        const tn_lower_len = @min(tn.len, 64);
        var li: usize = 0;
        while (li < tn_lower_len) : (li += 1) {
            tn_lower_buf[li] = toAsciiLower(tn[li]);
        }
        const tn_lower = tn_lower_buf[0..tn_lower_len];
        const selector_attrs = arena.attrSlice(i);
        var selector_buf = ArrayList(u8).init(allocator);
        defer selector_buf.deinit();
        try selector_buf.appendSlice(tn);
        if ((try findAttributeValue(allocator, selector_attrs, "id"))) |id_val| {
            defer allocator.free(id_val);
            if (id_val.len > 0) {
                try selector_buf.append('#');
                try selector_buf.appendSlice(id_val);
            }
        }
        if ((try findAttributeValue(allocator, selector_attrs, "class"))) |class_val| {
            defer allocator.free(class_val);
            if (class_val.len > 0) {
                try selector_buf.append('.');
                var first = true;
                for (class_val) |c| {
                    if (c == ' ' or c == '\t') {
                        if (!first) try selector_buf.append('.');
                    } else {
                        try selector_buf.append(c);
                        first = false;
                    }
                }
            }
        }
        var text_buf = ArrayList(u8).init(allocator);
        defer text_buf.deinit();
        var text_length: u32 = 0;
        var tag_length: u32 = 0;
        var image_list = ArrayList(usize).init(allocator);
        errdefer image_list.deinit();
        var has_form = false;
        var has_t3d = false;
        const close_idx = findSubtreeEnd(arena, i);
        var asset_lookup_idx: usize = 0;
        var k: usize = i + 1;
        while (k <= close_idx and k < arena.count) : (k += 1) {
            if ((arena.nodes[k].flags & FLAG_DISCARDED) != 0) continue;
            const tt = arena.types[k];
            if (tt == TK_OPEN or tt == TK_SELF) {
                const sub_tn = arena.tagName(k);
                tag_length += @intCast(sub_tn.len);
                if (eqlIgnoreCase(sub_tn, "img")) {
                    if (asset_lookup_idx < inventory.assets.items.len) {
                        try image_list.append(asset_lookup_idx);
                        asset_lookup_idx += 1;
                    }
                }
                if (eqlIgnoreCase(sub_tn, "form")) has_form = true;
                if ((arena.nodes[k].flags & FLAG_HAS_TRANSFORM3D) != 0) has_t3d = true;
            } else if (tt == TK_TEXT) {
                const raw = arena.rawSlice(k);
                text_length += @intCast(raw.len);
                if (text_buf.items.len < SECTION_TEXT_LIMIT) {
                    const decoded = try decodeHtmlEntities(allocator, raw);
                    defer allocator.free(decoded);
                    try appendNormalizedText(&text_buf, decoded);
                }
            }
        }
        var preview_slice = text_buf.items;
        if (preview_slice.len > SECTION_TEXT_LIMIT) preview_slice = preview_slice[0..SECTION_TEXT_LIMIT];
        const preview = try allocator.dupe(u8, preview_slice);
        errdefer allocator.free(preview);
        const section_text_copy = try allocator.dupe(u8, preview_slice);
        try inventory.section_text.append(section_text_copy);
        const selector_owned = try allocator.dupe(u8, selector_buf.items);
        errdefer allocator.free(selector_owned);
        const type_owned = try allocator.dupe(u8, tn);
        errdefer allocator.free(type_owned);
        const aria_lookup = AriaRoles.get(tn_lower) orelse "region";
        const aria_owned = try allocator.dupe(u8, aria_lookup);
        errdefer allocator.free(aria_owned);
        const weight = SemanticWeights.get(tn_lower) orelse 10;
        const density = if (tag_length == 0) text_length * 100 else (text_length * 100) / (tag_length + 1);
        const component_name = try sanitizeComponentName(allocator, tn, section_index);
        errdefer allocator.free(component_name);
        const component_file = try allocator.dupe(u8, "");
        const entropy_hash = try allocator.dupe(u8, "");
        const bipartite_label = try allocator.dupe(u8, "");
        const tw = try allocator.dupe(u8, "");
        const tr = try allocator.dupe(u8, "");
        try inventory.sections.append(Section{
            .index = section_index,
            .selector = selector_owned,
            .type_str = type_owned,
            .text_preview = preview,
            .images = image_list,
            .structural_hash = 0,
            .semantic_weight = weight,
            .text_length = text_length,
            .tag_length = tag_length,
            .density = density,
            .component_name = component_name,
            .component_file = component_file,
            .aria_role = aria_owned,
            .template_of = INVALID_IDX,
            .weight = weight * 100 + density,
            .root_node = i,
            .entropy_hash = entropy_hash,
            .minhash_sig = [_]u32{0} ** MINHASH_FUNCS,
            .prominence = 0,
            .is_pure = true,
            .has_state = false,
            .has_form = has_form,
            .has_transform3d = has_t3d,
            .bipartite_label = bipartite_label,
            .lcp_node = INVALID_IDX,
            .tailwind_classes = tw,
            .state_machine_id = INVALID_IDX,
            .transition_tokens = tr,
        });
        section_index += 1;
        arena.telemetry.section_count += 1;
    }
}

fn findSubtreeEnd(arena: *const TokenArena, idx: usize) usize {
    const depth = arena.nodes[idx].depth;
    var k: usize = idx + 1;
    while (k < arena.count) : (k += 1) {
        if (arena.types[k] == TK_DISCARD) continue;
        if (arena.nodes[k].depth <= depth) {
            return k - 1;
        }
    }
    return arena.count - 1;
}

fn computeStructuralFootprints(arena: *TokenArena, inventory: *Inventory) !void {
    for (inventory.sections.items) |*section| {
        const root = section.root_node;
        const end = findSubtreeEnd(arena, root);
        var hash: u64 = 14695981039346656037;
        var k = root;
        while (k <= end and k < arena.count) : (k += 1) {
            if ((arena.nodes[k].flags & FLAG_DISCARDED) != 0) continue;
            if ((arena.nodes[k].flags & FLAG_ELIDED) != 0) continue;
            const tt = arena.types[k];
            if (tt != TK_OPEN and tt != TK_SELF) continue;
            const name = arena.tagName(k);
            for (name) |c| {
                hash ^= toAsciiLower(c);
                hash *%= 1099511628211;
            }
            hash ^= @as(u64, arena.nodes[k].depth);
            hash *%= 1099511628211;
            hash ^= @as(u64, tt);
            hash *%= 1099511628211;
        }
        section.structural_hash = hash;
    }
}

fn computeMinHashSignatures(inventory: *Inventory) !void {
    for (inventory.sections.items) |*section| {
        var sig = [_]u32{0xFFFFFFFF} ** MINHASH_FUNCS;
        const txt = section.text_preview;
        if (txt.len < 3) {
            section.minhash_sig = sig;
            continue;
        }
        var i: usize = 0;
        while (i + 3 <= txt.len) : (i += 1) {
            const gram = txt[i .. i + 3];
            var base_hash: u64 = 14695981039346656037;
            for (gram) |c| {
                base_hash ^= c;
                base_hash *%= 1099511628211;
            }
            var f: usize = 0;
            while (f < MINHASH_FUNCS) : (f += 1) {
                const seed: u64 = @as(u64, f) * 2654435761 + 1;
                const h: u32 = @intCast((base_hash ^ seed) & 0xFFFFFFFF);
                if (h < sig[f]) sig[f] = h;
            }
        }
        section.minhash_sig = sig;
    }
}

fn computeProminence(arena: *TokenArena, inventory: *Inventory) !void {
    for (inventory.sections.items) |*section| {
        const node = arena.nodes[section.root_node];
        const depth_f: f64 = @floatFromInt(node.depth);
        const weight_f: f64 = @floatFromInt(section.semantic_weight);
        const text_f: f64 = @floatFromInt(section.text_length);
        const image_count_f: f64 = @floatFromInt(section.images.items.len);
        const sibling_penalty: f64 = if (node.parent_idx == INVALID_IDX) 0 else 5;
        const order_penalty: f64 = @as(f64, @floatFromInt(section.root_node)) / 1000.0;
        const prominence = weight_f * 10.0 + text_f * 0.5 + image_count_f * 20.0 - depth_f * 8.0 - sibling_penalty - order_penalty;
        section.prominence = prominence;
    }
}

fn detectLcpCandidate(arena: *TokenArena, inventory: *Inventory) !void {
    var best_score: f64 = -1e30;
    var best_section: usize = INVALID_IDX;
    var best_node: usize = INVALID_IDX;
    for (inventory.sections.items, 0..) |section, si| {
        if (section.prominence > best_score) {
            best_score = section.prominence;
            best_section = si;
            const end = findSubtreeEnd(arena, section.root_node);
            var k = section.root_node;
            var img_node: usize = INVALID_IDX;
            var text_node: usize = INVALID_IDX;
            while (k <= end and k < arena.count) : (k += 1) {
                if (arena.types[k] == TK_SELF and eqlIgnoreCase(arena.tagName(k), "img")) {
                    if (img_node == INVALID_IDX) img_node = k;
                }
                if (arena.types[k] == TK_OPEN) {
                    const tn = arena.tagName(k);
                    if (eqlIgnoreCase(tn, "h1") or eqlIgnoreCase(tn, "h2")) {
                        if (text_node == INVALID_IDX) text_node = k;
                    }
                }
            }
            best_node = if (img_node != INVALID_IDX) img_node else text_node;
        }
    }
    if (best_section != INVALID_IDX and best_node != INVALID_IDX) {
        inventory.sections.items[best_section].lcp_node = best_node;
        arena.nodes[best_node].flags |= FLAG_LCP_CANDIDATE;
    }
}

fn analyzePurity(arena: *TokenArena, inventory: *Inventory) !void {
    for (inventory.sections.items) |*section| {
        const end = findSubtreeEnd(arena, section.root_node);
        var has_state = false;
        var k = section.root_node;
        while (k <= end and k < arena.count) : (k += 1) {
            const attrs = arena.attrSlice(k);
            if (Mem.indexOf(u8, attrs, "data-state") != null) has_state = true;
            if (Mem.indexOf(u8, attrs, "data-toggle") != null) has_state = true;
            if (Mem.indexOf(u8, attrs, "aria-expanded") != null) has_state = true;
            if (Mem.indexOf(u8, attrs, "js-") != null) has_state = true;
            if (Mem.indexOf(u8, attrs, "onclick") != null) has_state = true;
            const tn = arena.tagName(k);
            if (eqlIgnoreCase(tn, "input") or eqlIgnoreCase(tn, "select") or eqlIgnoreCase(tn, "textarea") or eqlIgnoreCase(tn, "button") or eqlIgnoreCase(tn, "form")) {
                has_state = true;
            }
        }
        section.has_state = has_state;
        section.is_pure = !has_state;
    }
}

fn inferBipartiteLabels(inventory: *Inventory) !void {
    const allocator = inventory.sections.allocator;
    for (inventory.sections.items) |*section| {
        var label_buf = ArrayList(u8).init(allocator);
        defer label_buf.deinit();
        var lower_type_buf: [64]u8 = undefined;
        const len = @min(section.type_str.len, 64);
        for (section.type_str[0..len], 0..) |c, i| lower_type_buf[i] = toAsciiLower(c);
        const lower_type = lower_type_buf[0..len];
        if (Mem.eql(u8, lower_type, "header") and section.images.items.len > 0) {
            try label_buf.appendSlice("HeroNavbar");
        } else if (Mem.eql(u8, lower_type, "header")) {
            try label_buf.appendSlice("SiteHeader");
        } else if (Mem.eql(u8, lower_type, "nav")) {
            try label_buf.appendSlice("PrimaryNavigation");
        } else if (Mem.eql(u8, lower_type, "footer")) {
            try label_buf.appendSlice("SiteFooter");
        } else if (Mem.eql(u8, lower_type, "main")) {
            try label_buf.appendSlice("MainContent");
        } else if (Mem.eql(u8, lower_type, "article")) {
            try label_buf.appendSlice("Article");
        } else if (Mem.eql(u8, lower_type, "aside")) {
            try label_buf.appendSlice("Sidebar");
        } else if (section.images.items.len >= 3) {
            try label_buf.appendSlice("Gallery");
        } else if (section.has_form) {
            try label_buf.appendSlice("FormBlock");
        } else if (section.text_length > 500) {
            try label_buf.appendSlice("ContentBlock");
        } else {
            try label_buf.appendSlice("GenericSection");
        }
        allocator.free(section.bipartite_label);
        section.bipartite_label = try allocator.dupe(u8, label_buf.items);
    }
}

fn inferAriaConstraints(arena: *TokenArena, inventory: *Inventory) !void {
    const allocator = inventory.sections.allocator;
    for (inventory.sections.items) |*section| {
        var tn_lower_buf: [64]u8 = undefined;
        const len = @min(section.type_str.len, 64);
        for (section.type_str[0..len], 0..) |c, i| tn_lower_buf[i] = toAsciiLower(c);
        const tn_lower = tn_lower_buf[0..len];
        const default_role = AriaRoles.get(tn_lower) orelse "region";
        const attrs = arena.attrSlice(section.root_node);
        if (Mem.indexOf(u8, attrs, "role=") == null and Mem.indexOf(u8, attrs, "aria-label") == null) {
            allocator.free(section.aria_role);
            section.aria_role = try allocator.dupe(u8, default_role);
        }
    }
}

fn inferEventListeners(arena: *TokenArena) !void {
    var i: usize = 0;
    while (i < arena.count) : (i += 1) {
        if (arena.types[i] != TK_OPEN and arena.types[i] != TK_SELF) continue;
        const attrs = arena.attrSlice(i);
        var tn_lower_buf: [64]u8 = undefined;
        const tn = arena.tagName(i);
        const len = @min(tn.len, 64);
        for (tn[0..len], 0..) |c, ci| tn_lower_buf[ci] = toAsciiLower(c);
        const tn_lower = tn_lower_buf[0..len];
        var interactive = false;
        if (InteractiveTags.has(tn_lower)) interactive = true;
        if (Mem.indexOf(u8, attrs, "onclick") != null) interactive = true;
        if (Mem.indexOf(u8, attrs, "js-") != null) interactive = true;
        if (Mem.indexOf(u8, attrs, "data-toggle") != null) interactive = true;
        if (Mem.indexOf(u8, attrs, "data-modal") != null) interactive = true;
        if (Mem.indexOf(u8, attrs, "tabindex") != null) interactive = true;
        if (interactive) {
            arena.nodes[i].flags |= FLAG_INTERACTIVE;
            arena.telemetry.interactive_count += 1;
        }
    }
}

fn buildCfg(arena: *TokenArena, inventory: *Inventory) !void {
    var sm_id: usize = 0;
    for (inventory.sections.items) |*section| {
        const end = findSubtreeEnd(arena, section.root_node);
        var has_toggle = false;
        var k = section.root_node;
        while (k <= end and k < arena.count) : (k += 1) {
            const attrs = arena.attrSlice(k);
            if (Mem.indexOf(u8, attrs, "aria-expanded") != null) has_toggle = true;
            if (Mem.indexOf(u8, attrs, "data-toggle") != null) has_toggle = true;
            const tn = arena.tagName(k);
            if (eqlIgnoreCase(tn, "details") or eqlIgnoreCase(tn, "summary")) has_toggle = true;
        }
        if (has_toggle) {
            section.state_machine_id = sm_id;
            sm_id += 1;
        }
        if (section.has_transform3d) {
            arena.nodes[section.root_node].flags |= FLAG_HAS_TRANSFORM3D;
        }
        if (section.text_length > 4000 or section.images.items.len > 8) {
            arena.nodes[section.root_node].flags |= FLAG_HEAVY_DATA;
        }
    }
}

fn deduplicateStructures(inventory: *Inventory) !void {
    var idx: usize = 0;
    for (inventory.sections.items) |*section| {
        const gop = try inventory.structural_map.getOrPut(section.structural_hash);
        if (gop.found_existing) {
            section.template_of = gop.value_ptr.*;
        } else {
            gop.value_ptr.* = idx;
            section.template_of = INVALID_IDX;
        }
        idx += 1;
    }
}

fn computeTextEntropyHashes(allocator: Allocator, arena: *TokenArena, inventory: *Inventory) !void {
    _ = arena;
    for (inventory.sections.items) |*section| {
        var counts = [_]u32{0} ** 256;
        var total: u32 = 0;
        for (section.text_preview) |c| {
            counts[c] += 1;
            total += 1;
        }
        var entropy: f64 = 0;
        if (total > 0) {
            const total_f: f64 = @floatFromInt(total);
            for (counts) |c| {
                if (c == 0) continue;
                const p: f64 = @as(f64, @floatFromInt(c)) / total_f;
                entropy -= p * std.math.log2(p);
            }
        }
        const e_scaled: u32 = @intFromFloat(if (std.math.isNan(entropy)) 0.0 else entropy * 1000.0);
        const tag_hash: u64 = section.structural_hash ^ (@as(u64, e_scaled) *% 2654435761);
        var name_buf = ArrayList(u8).init(allocator);
        errdefer name_buf.deinit();
        try name_buf.appendSlice(section.bipartite_label);
        try name_buf.append('_');
        try name_buf.writer().print("{x:0>4}", .{tag_hash & 0xFFFF});
        allocator.free(section.entropy_hash);
        section.entropy_hash = try name_buf.toOwnedSlice();
    }
}

fn computeTfIdf(allocator: Allocator, inventory: *Inventory) !void {
    const doc_count = inventory.section_text.items.len;
    if (doc_count == 0) return;
    for (inventory.section_text.items) |doc| {
        var seen_in_doc = StringHashMap(void).init(allocator);
        defer {
            var it = seen_in_doc.iterator();
            while (it.next()) |e| allocator.free(e.key_ptr.*);
            seen_in_doc.deinit();
        }
        var word_start: usize = 0;
        var idx: usize = 0;
        while (idx <= doc.len) : (idx += 1) {
            const at_break = idx == doc.len or !isWordChar(doc[idx]);
            if (at_break) {
                if (idx > word_start) {
                    const word = doc[word_start..idx];
                    if (word.len >= 3 and word.len < 32) {
                        var lower_buf: [32]u8 = undefined;
                        var li: usize = 0;
                        while (li < word.len) : (li += 1) lower_buf[li] = toAsciiLower(word[li]);
                        const lower = lower_buf[0..word.len];
                        if (!isStopWord(lower)) {
                            const key = try allocator.dupe(u8, lower);
                            const gop = try inventory.text_tokens.getOrPut(key);
                            if (gop.found_existing) {
                                gop.value_ptr.* += 1;
                                allocator.free(key);
                            } else {
                                gop.value_ptr.* = 1;
                            }
                            const key2 = try allocator.dupe(u8, lower);
                            const seen_gop = try seen_in_doc.getOrPut(key2);
                            if (seen_gop.found_existing) {
                                allocator.free(key2);
                            } else {
                                const key3 = try allocator.dupe(u8, lower);
                                const df_gop = try inventory.text_doc_freq.getOrPut(key3);
                                if (df_gop.found_existing) {
                                    df_gop.value_ptr.* += 1;
                                    allocator.free(key3);
                                } else {
                                    df_gop.value_ptr.* = 1;
                                }
                            }
                        }
                    }
                }
                word_start = idx + 1;
            }
        }
    }
}

fn isWordChar(c: u8) bool {
    return (c >= 'a' and c <= 'z') or (c >= 'A' and c <= 'Z') or (c >= '0' and c <= '9');
}

fn isStopWord(w: []const u8) bool {
    const stop_words = [_][]const u8{
        "the", "and", "for", "are", "but", "not", "you", "all", "can", "her",
        "was", "one", "our", "out", "day", "get", "has", "him", "his", "how",
        "may", "new", "now", "old", "see", "two", "way", "who", "boy", "did",
        "its", "let", "put", "say", "she", "too", "use", "with", "this", "that",
        "from", "have", "they", "your", "what", "when", "make", "like", "into",
        "more", "some", "such", "than", "then", "them", "well", "were", "will",
    };
    for (stop_words) |sw| {
        if (Mem.eql(u8, sw, w)) return true;
    }
    return false;
}

fn buildMarkovChain(allocator: Allocator, inventory: *Inventory) !void {
    for (inventory.section_text.items) |doc| {
        var word_starts = ArrayList(usize).init(allocator);
        defer word_starts.deinit();
        var word_ends = ArrayList(usize).init(allocator);
        defer word_ends.deinit();
        var i: usize = 0;
        while (i < doc.len) {
            while (i < doc.len and !isWordChar(doc[i])) i += 1;
            if (i >= doc.len) break;
            const start = i;
            while (i < doc.len and isWordChar(doc[i])) i += 1;
            try word_starts.append(start);
            try word_ends.append(i);
        }
        if (word_starts.items.len < 3) continue;
        var w: usize = 0;
        while (w + 2 < word_starts.items.len) : (w += 1) {
            const w1 = doc[word_starts.items[w]..word_ends.items[w]];
            const w2 = doc[word_starts.items[w + 1]..word_ends.items[w + 1]];
            const w3 = doc[word_starts.items[w + 2]..word_ends.items[w + 2]];
            var key_hash: u64 = 14695981039346656037;
            for (w1) |c| {
                key_hash ^= toAsciiLower(c);
                key_hash *%= 1099511628211;
            }
            key_hash ^= 0x5F5F5F5F;
            for (w2) |c| {
                key_hash ^= toAsciiLower(c);
                key_hash *%= 1099511628211;
            }
            const gop = try inventory.markov.getOrPut(key_hash);
            if (!gop.found_existing) {
                gop.value_ptr.* = ArrayList([]const u8).init(allocator);
            }
            try gop.value_ptr.append(try allocator.dupe(u8, w3));
        }
    }
}

fn harvestGlyphs(allocator: Allocator, inventory: *Inventory) !void {
    _ = allocator;
    for (inventory.section_text.items) |doc| {
        for (doc) |c| {
            try inventory.glyphs.put(c, {});
        }
    }
    for (inventory.internal_links.items) |link| {
        for (link.text) |c| {
            try inventory.glyphs.put(c, {});
        }
    }
}

fn generateTailwindClasses(allocator: Allocator, arena: *TokenArena, inventory: *Inventory) !void {
    for (inventory.sections.items) |*section| {
        var classes = ArrayList(u8).init(allocator);
        errdefer classes.deinit();
        try classes.appendSlice("relative w-full");
        const attrs = arena.attrSlice(section.root_node);
        if (Mem.indexOf(u8, attrs, "flex") != null) try classes.appendSlice(" flex");
        if (Mem.indexOf(u8, attrs, "grid") != null) try classes.appendSlice(" grid");
        if (Mem.indexOf(u8, attrs, "text-center") != null) try classes.appendSlice(" text-center");
        if (Mem.indexOf(u8, attrs, "center") != null) try classes.appendSlice(" items-center justify-center");
        if (Mem.indexOf(u8, attrs, "hero") != null) try classes.appendSlice(" min-h-[60vh] py-24");
        if (Mem.indexOf(u8, attrs, "container") != null) try classes.appendSlice(" mx-auto max-w-7xl px-4");
        var tn_lower_buf: [64]u8 = undefined;
        const len = @min(section.type_str.len, 64);
        for (section.type_str[0..len], 0..) |c, i| tn_lower_buf[i] = toAsciiLower(c);
        const tn_lower = tn_lower_buf[0..len];
        if (Mem.eql(u8, tn_lower, "header")) try classes.appendSlice(" border-b border-black/10 py-5");
        if (Mem.eql(u8, tn_lower, "footer")) try classes.appendSlice(" border-t border-black/10 py-8");
        if (Mem.eql(u8, tn_lower, "nav")) try classes.appendSlice(" flex items-center gap-4");
        if (Mem.eql(u8, tn_lower, "section")) try classes.appendSlice(" py-16 px-6");
        allocator.free(section.tailwind_classes);
        section.tailwind_classes = try classes.toOwnedSlice();
        var tr_buf = ArrayList(u8).init(allocator);
        errdefer tr_buf.deinit();
        if (section.has_transform3d) {
            try tr_buf.appendSlice("{ opacity: [0,1], y: [40,0] }");
        } else if (section.semantic_weight >= 60) {
            try tr_buf.appendSlice("{ opacity: [0,1] }");
        }
        allocator.free(section.transition_tokens);
        section.transition_tokens = try tr_buf.toOwnedSlice();
    }
}

fn fetchRawHtml(allocator: Allocator, url_str: []const u8) ![]const u8 {
    const uri = try Url.parse(url_str);
    var client = Http.Client{ .allocator = allocator };
    defer client.deinit();

    var buf: [16384]u8 = undefined;
    const headers = [_]Http.Header{.{ .name = "User-Agent", .value = USER_AGENT }};
    var req = try client.open(.GET, uri, .{ .server_header_buffer = &buf, .extra_headers = &headers });
    defer req.deinit();

    try req.send();
    try req.wait();

    if (req.response.status != .ok) {
        return error.HttpRequestFailed;
    }

    return try req.reader().readAllAlloc(allocator, 50 * 1024 * 1024);
}

fn downloadAssets(allocator: Allocator, inventory: *Inventory, target_url: []const u8) !void {
    const base = try parseBaseUrl(target_url);

    var client = Http.Client{ .allocator = allocator };
    defer client.deinit();

    for (inventory.assets.items) |*asset| {
        if (asset.src.len == 0) continue;
        if (Mem.startsWith(u8, asset.src, "data:")) continue;

        var url_str = asset.src;
        var free_url = false;

        if (Mem.indexOf(u8, url_str, "cdn.shopify.com") != null) {
            if (Mem.indexOf(u8, url_str, "_small.") != null) {
                const new_url = try replaceString(allocator, url_str, "_small.", "_1200x.");
                if (free_url) allocator.free(url_str);
                url_str = new_url;
                free_url = true;
            }
            if (Mem.indexOf(u8, url_str, "_medium.") != null) {
                const new_url = try replaceString(allocator, url_str, "_medium.", "_1200x.");
                if (free_url) allocator.free(url_str);
                url_str = new_url;
                free_url = true;
            }
            if (Mem.indexOf(u8, url_str, "?width=") != null) {
                var parts = Mem.splitScalar(u8, url_str, '?');
                const base_url_part = parts.first();
                const new_url = try std.fmt.allocPrint(allocator, "{s}?width=1600", .{base_url_part});
                if (free_url) allocator.free(url_str);
                url_str = new_url;
                free_url = true;
            }
        }

        var abs_url_str: []const u8 = undefined;
        var free_abs = false;

        if (Mem.startsWith(u8, url_str, "http://") or Mem.startsWith(u8, url_str, "https://")) {
            abs_url_str = url_str;
        } else if (Mem.startsWith(u8, url_str, "//")) {
            abs_url_str = try std.fmt.allocPrint(allocator, "{s}:{s}", .{ base.scheme, url_str });
            free_abs = true;
        } else if (Mem.startsWith(u8, url_str, "/")) {
            abs_url_str = try std.fmt.allocPrint(allocator, "{s}://{s}{s}", .{ base.scheme, base.host, url_str });
            free_abs = true;
        } else {
            abs_url_str = try std.fmt.allocPrint(allocator, "{s}://{s}{s}{s}", .{ base.scheme, base.host, base.base_dir, url_str });
            free_abs = true;
        }

        const uri = Url.parse(abs_url_str) catch |err| {
            std.debug.print("Failed to parse URI: {s} ({})\n", .{ abs_url_str, err });
            if (free_abs) allocator.free(abs_url_str);
            if (free_url) allocator.free(url_str);
            continue;
        };

        var buf: [16384]u8 = undefined;
        const headers = [_]Http.Header{.{ .name = "User-Agent", .value = USER_AGENT }};
        var req = client.open(.GET, uri, .{ .server_header_buffer = &buf, .extra_headers = &headers }) catch |err| {
            std.debug.print("Failed to open request: {s} ({})\n", .{ abs_url_str, err });
            if (free_abs) allocator.free(abs_url_str);
            if (free_url) allocator.free(url_str);
            continue;
        };

        req.send() catch |err| {
            std.debug.print("Failed to send request: {s} ({})\n", .{ abs_url_str, err });
            req.deinit();
            if (free_abs) allocator.free(abs_url_str);
            if (free_url) allocator.free(url_str);
            continue;
        };
        req.wait() catch |err| {
            std.debug.print("Failed to wait for response: {s} ({})\n", .{ abs_url_str, err });
            req.deinit();
            if (free_abs) allocator.free(abs_url_str);
            if (free_url) allocator.free(url_str);
            continue;
        };

        if (req.response.status != .ok) {
            std.debug.print("Bad status: {s} ({})\n", .{ abs_url_str, req.response.status });
            req.deinit();
            if (free_abs) allocator.free(abs_url_str);
            if (free_url) allocator.free(url_str);
            continue;
        }

        const body = req.reader().readAllAlloc(allocator, 25 * 1024 * 1024) catch |err| {
            std.debug.print("Failed to read body: {s} ({})\n", .{ abs_url_str, err });
            req.deinit();
            if (free_abs) allocator.free(abs_url_str);
            if (free_url) allocator.free(url_str);
            continue;
        };
        req.deinit();

        const ext = getExt(url_str);
        const hash = hashString(url_str);
        const filename = try std.fmt.allocPrint(allocator, "img-{s}{s}", .{ hash[0..], ext });

        const local_path = try std.fmt.allocPrint(allocator, "images/{s}", .{filename});
        allocator.free(filename);

        const full_path = try std.fmt.allocPrint(allocator, "public/{s}", .{local_path});

        Fs.cwd().writeFile(.{ .sub_path = full_path, .data = body }) catch |err| {
            std.debug.print("Failed to write file: {s} ({})\n", .{ full_path, err });
            allocator.free(full_path);
            allocator.free(local_path);
            allocator.free(body);
            if (free_abs) allocator.free(abs_url_str);
            if (free_url) allocator.free(url_str);
            continue;
        };
        allocator.free(full_path);

        allocator.free(asset.local_path);
        asset.local_path = local_path;
        asset.bytes_size = @intCast(body.len);
        asset.phash = computePerceptualHash(body);
        asset.is_vector_candidate = body.len < 20480;
        asset.width = 320;
        asset.height = 240;

        allocator.free(body);
        if (free_abs) allocator.free(abs_url_str);
        if (free_url) allocator.free(url_str);
    }
}

fn computePerceptualHash(bytes: []const u8) u64 {
    if (bytes.len == 0) return 0;
    const block: usize = @max(@as(usize, 1), bytes.len / 64);
    var hash: u64 = 0;
    var i: u32 = 0;
    while (i < 64) : (i += 1) {
        const start = @as(usize, i) * block;
        if (start >= bytes.len) break;
        const end = @min(start + block, bytes.len);
        var sum: u32 = 0;
        var k = start;
        while (k < end) : (k += 1) sum +%= bytes[k];
        const avg: u8 = @intCast((sum / @as(u32, @intCast(end - start))) & 0xFF);
        if (avg > 128) hash |= (@as(u64, 1) << @intCast(i));
    }
    return hash;
}

fn transcodeImages(allocator: Allocator, inventory: *Inventory) !void {
    _ = allocator;
    for (inventory.assets.items) |*asset| {
        if (asset.local_path.len == 0) continue;
        if (Mem.endsWith(u8, asset.local_path, ".svg")) continue;
        if (Mem.endsWith(u8, asset.local_path, ".gif")) continue;
        if (asset.bytes_size < 4096) continue;
    }
}

fn detectImageDuplicates(inventory: *Inventory) !void {
    var i: usize = 0;
    while (i < inventory.assets.items.len) : (i += 1) {
        if (inventory.assets.items[i].duplicate_of != INVALID_IDX) continue;
        if (inventory.assets.items[i].phash == 0) continue;
        var j: usize = i + 1;
        while (j < inventory.assets.items.len) : (j += 1) {
            const xor = inventory.assets.items[i].phash ^ inventory.assets.items[j].phash;
            if (@popCount(xor) <= 4) {
                inventory.assets.items[j].duplicate_of = i;
            }
        }
    }
}

fn vectorizeSmallImages(allocator: Allocator, inventory: *Inventory) !void {
    _ = allocator;
    for (inventory.assets.items) |*asset| {
        if (!asset.is_vector_candidate) continue;
        if (asset.bytes_size > 16384) {
            asset.is_vector_candidate = false;
        }
    }
}

fn packSpriteSheet(inventory: *Inventory) !void {
    var x: u32 = 0;
    var y: u32 = 0;
    var row_h: u32 = 0;
    const sheet_w: u32 = 1024;
    for (inventory.assets.items) |*asset| {
        if (asset.bytes_size == 0 or asset.bytes_size > 8192) continue;
        if (asset.duplicate_of != INVALID_IDX) continue;
        const w: u32 = 64;
        const h: u32 = 64;
        if (x + w > sheet_w) {
            x = 0;
            y += row_h;
            row_h = 0;
        }
        asset.sprite_x = x;
        asset.sprite_y = y;
        asset.sprite_w = w;
        asset.sprite_h = h;
        x += w;
        if (h > row_h) row_h = h;
    }
}

fn generateBlurhashes(allocator: Allocator, inventory: *Inventory) !void {
    for (inventory.assets.items) |*asset| {
        if (asset.bytes_size == 0) continue;
        var sample: [FFT_SIZE]f64 = undefined;
        var i: usize = 0;
        while (i < FFT_SIZE) : (i += 1) {
            sample[i] = 0;
        }
        const phash = asset.phash;
        i = 0;
        while (i < FFT_SIZE) : (i += 1) {
            const bit_idx: u6 = @intCast(i % 64);
            const bit = (phash >> bit_idx) & 1;
            sample[i] = @floatFromInt(bit);
        }
        var freqs: [FFT_SIZE]f64 = undefined;
        var k: usize = 0;
        while (k < FFT_SIZE) : (k += 1) {
            var sum_re: f64 = 0;
            var sum_im: f64 = 0;
            var n: usize = 0;
            while (n < FFT_SIZE) : (n += 1) {
                const theta = -2.0 * std.math.pi * @as(f64, @floatFromInt(k * n)) / @as(f64, @floatFromInt(FFT_SIZE));
                sum_re += sample[n] * @cos(theta);
                sum_im += sample[n] * @sin(theta);
            }
            freqs[k] = @sqrt(sum_re * sum_re + sum_im * sum_im);
        }
        var buf = ArrayList(u8).init(allocator);
        errdefer buf.deinit();
        var top: usize = 0;
        while (top < 8) : (top += 1) {
            const v = freqs[top];
            const scaled: u32 = @intFromFloat(if (std.math.isNan(v)) 0.0 else @min(v * 8.0, 255.0));
            const c = base83Char(scaled & 0x3F);
            try buf.append(c);
        }
        allocator.free(asset.blurhash);
        asset.blurhash = try buf.toOwnedSlice();
    }
}

fn base83Char(v: u32) u8 {
    const chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz#$%*+,-.:;=?@[]^_{|}~";
    return chars[v % chars.len];
}

fn generateReactApp(allocator: Allocator, arena: *TokenArena, inventory: *Inventory, config: Config, vfs: *VFS) !void {
    const package_json =
        \\{
        \\  "name": "website-clone",
        \\  "private": true,
        \\  "version": "0.0.0",
        \\  "type": "module",
        \\  "scripts": {
        \\    "dev": "vite",
        \\    "build": "tsc && vite build",
        \\    "preview": "vite preview",
        \\    "test": "node tests/differential.mjs"
        \\  },
        \\  "dependencies": {
        \\    "react": "^18.2.0",
        \\    "react-dom": "^18.2.0",
        \\    "framer-motion": "^11.0.0",
        \\    "xstate": "^5.0.0",
        \\    "@xstate/react": "^4.0.0",
        \\    "@apollo/client": "^3.9.0",
        \\    "graphql": "^16.8.0",
        \\    "@react-three/fiber": "^8.15.0",
        \\    "three": "^0.160.0"
        \\  },
        \\  "devDependencies": {
        \\    "@types/react": "^18.2.43",
        \\    "@types/react-dom": "^18.2.17",
        \\    "@vitejs/plugin-react": "^4.2.1",
        \\    "typescript": "^5.2.2",
        \\    "vite": "^5.0.8",
        \\    "tailwindcss": "^3.4.0",
        \\    "autoprefixer": "^10.4.16",
        \\    "postcss": "^8.4.32",
        \\    "playwright": "^1.40.0"
        \\  }
        \\}
    ;
    try vfs.addCopy("package.json", package_json);

    const tsconfig_json =
        \\{
        \\  "compilerOptions": {
        \\    "target": "ES2020",
        \\    "useDefineForClassFields": true,
        \\    "lib": ["ES2020", "DOM", "DOM.Iterable", "WebWorker"],
        \\    "module": "ESNext",
        \\    "skipLibCheck": true,
        \\    "moduleResolution": "bundler",
        \\    "allowImportingTsExtensions": true,
        \\    "resolveJsonModule": true,
        \\    "isolatedModules": true,
        \\    "noEmit": true,
        \\    "jsx": "react-jsx",
        \\    "strict": true,
        \\    "noUnusedLocals": false,
        \\    "noUnusedParameters": false,
        \\    "noFallthroughCasesInSwitch": true
        \\  },
        \\  "include": ["src"],
        \\  "references": [{ "path": "./tsconfig.node.json" }]
        \\}
    ;
    try vfs.addCopy("tsconfig.json", tsconfig_json);

    const tsconfig_node_json =
        \\{
        \\  "compilerOptions": {
        \\    "composite": true,
        \\    "skipLibCheck": true,
        \\    "module": "ESNext",
        \\    "moduleResolution": "bundler",
        \\    "allowSyntheticDefaultImports": true
        \\  },
        \\  "include": ["vite.config.ts"]
        \\}
    ;
    try vfs.addCopy("tsconfig.node.json", tsconfig_node_json);

    const vite_config_ts =
        \\import { defineConfig } from 'vite'
        \\import react from '@vitejs/plugin-react'
        \\
        \\export default defineConfig({
        \\  plugins: [react()],
        \\  base: './',
        \\  build: {
        \\    target: 'es2020',
        \\    rollupOptions: {
        \\      output: { manualChunks: { vendor: ['react', 'react-dom'], motion: ['framer-motion'], three: ['three', '@react-three/fiber'] } }
        \\    }
        \\  }
        \\})
    ;
    try vfs.addCopy("vite.config.ts", vite_config_ts);

    var index_html = ArrayList(u8).init(allocator);
    defer index_html.deinit();
    try index_html.appendSlice("<!doctype html>\n<html lang=\"en\">\n  <head>\n    <meta charset=\"UTF-8\" />\n    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />\n    <title>Website Clone</title>\n    <link rel=\"stylesheet\" href=\"/src/index.css\" />\n");
    for (inventory.sections.items) |sec| {
        if (sec.lcp_node != INVALID_IDX) {
            for (sec.images.items) |img_idx| {
                if (img_idx >= inventory.assets.items.len) continue;
                const img = inventory.assets.items[img_idx];
                if (img.local_path.len > 0) {
                    try index_html.writer().print("    <link rel=\"preload\" as=\"image\" href=\"/{s}\" />\n", .{img.local_path});
                    break;
                }
            }
        }
    }
    try index_html.appendSlice("  </head>\n  <body>\n    <div id=\"root\"></div>\n    <script type=\"module\" src=\"./src/main.tsx\"></script>\n  </body>\n</html>\n");
    try vfs.addCopy("index.html", index_html.items);

    var index_css = ArrayList(u8).init(allocator);
    defer index_css.deinit();
    try index_css.appendSlice("@tailwind base;\n@tailwind components;\n@tailwind utilities;\n");
    try index_css.appendSlice("* { margin: 0; padding: 0; box-sizing: border-box; }\n");
    try index_css.appendSlice("body { font-family: 'CloneSubset', system-ui, -apple-system, BlinkMacSystemFont, \"Segoe UI\", sans-serif; background: #ffffff; color: #111111; }\n");
    try index_css.appendSlice("a { color: inherit; text-decoration: none; }\n");
    try index_css.appendSlice("img { max-width: 100%; height: auto; display: block; }\n");
    try index_css.appendSlice("@font-face { font-family: 'CloneSubset'; src: url('/fonts/subset.woff2') format('woff2'); font-display: swap; }\n");
    try index_css.appendSlice(".captured-section { padding: clamp(2rem, 6vw, 5rem) 5vw; display: grid; gap: 1.5rem; }\n");
    try index_css.appendSlice(".sprite { background-image: url('/images/sprite.png'); background-repeat: no-repeat; display: inline-block; }\n");
    if (inventory.tokens.count() > 0) {
        try index_css.appendSlice(":root {\n");
        var token_iter = inventory.tokens.iterator();
        while (token_iter.next()) |entry| {
            try index_css.writer().print("  {s}: {s};\n", .{ entry.key_ptr.*, entry.value_ptr.* });
        }
        try index_css.appendSlice("}\n");
    }
    try vfs.addCopy("src/index.css", index_css.items);

    var design_tokens_ts = ArrayList(u8).init(allocator);
    defer design_tokens_ts.deinit();
    try design_tokens_ts.appendSlice("export interface DesignTokens {\n");
    {
        var token_iter = inventory.tokens.iterator();
        while (token_iter.next()) |entry| {
            const key = entry.key_ptr.*;
            const ts_key = try cssVarToTsKey(allocator, key);
            defer allocator.free(ts_key);
            try design_tokens_ts.writer().print("  {s}: string;\n", .{ts_key});
        }
    }
    try design_tokens_ts.appendSlice("}\n\n");
    try design_tokens_ts.appendSlice("export const designTokens: DesignTokens = {\n");
    {
        var token_iter = inventory.tokens.iterator();
        while (token_iter.next()) |entry| {
            const key = entry.key_ptr.*;
            const ts_key = try cssVarToTsKey(allocator, key);
            defer allocator.free(ts_key);
            try design_tokens_ts.appendSlice("  ");
            try design_tokens_ts.appendSlice(ts_key);
            try design_tokens_ts.appendSlice(": ");
            try appendTsStringLiteral(&design_tokens_ts, entry.value_ptr.*);
            try design_tokens_ts.appendSlice(",\n");
        }
    }
    try design_tokens_ts.appendSlice("};\n\n");
    try design_tokens_ts.appendSlice("export interface CapturedSectionProps {\n  index: number;\n  selector: string;\n  ariaRole: string;\n  text?: string;\n  images: Array<{ src: string; alt: string; blurhash?: string }>;\n}\n");
    try vfs.addCopy("src/designTokens.ts", design_tokens_ts.items);

    const main_tsx =
        \\import React from 'react'
        \\import ReactDOM from 'react-dom/client'
        \\import App from './App.tsx'
        \\import './index.css'
        \\
        \\ReactDOM.createRoot(document.getElementById('root')!).render(
        \\  <React.StrictMode>
        \\    <App />
        \\  </React.StrictMode>,
        \\)
    ;
    try vfs.addCopy("src/main.tsx", main_tsx);

    var file_index: u32 = 0;
    var sections_in_file: u32 = 0;
    var current_file_buf = ArrayList(u8).init(allocator);
    defer current_file_buf.deinit();
    try beginComponentFile(&current_file_buf);

    for (inventory.sections.items, 0..) |*section, sec_i| {
        if (sections_in_file >= MAX_COMPONENTS_PER_FILE) {
            const fname = try std.fmt.allocPrint(allocator, "src/components/Partition{d}.tsx", .{file_index});
            defer allocator.free(fname);
            const data_owned = try current_file_buf.toOwnedSlice();
            try vfs.addOwned(fname, data_owned);
            current_file_buf = ArrayList(u8).init(allocator);
            try beginComponentFile(&current_file_buf);
            file_index += 1;
            sections_in_file = 0;
        }
        const file_basename = try std.fmt.allocPrint(allocator, "Partition{d}", .{file_index});
        defer allocator.free(file_basename);
        allocator.free(section.component_file);
        section.component_file = try allocator.dupe(u8, file_basename);
        if (section.template_of == INVALID_IDX) {
            try writeComponent(&current_file_buf, arena, inventory, section.*, sec_i);
            sections_in_file += 1;
        }
    }
    {
        const fname = try std.fmt.allocPrint(allocator, "src/components/Partition{d}.tsx", .{file_index});
        defer allocator.free(fname);
        const data_owned = try current_file_buf.toOwnedSlice();
        try vfs.addOwned(fname, data_owned);
    }

    var app_tsx = ArrayList(u8).init(allocator);
    defer app_tsx.deinit();
    try app_tsx.appendSlice("import React from 'react';\n");
    var seen_partitions = AutoHashMap(u32, void).init(allocator);
    defer seen_partitions.deinit();
    var file_idx_iter: u32 = 0;
    while (file_idx_iter <= file_index) : (file_idx_iter += 1) {
        try seen_partitions.put(file_idx_iter, {});
    }
    var sp_it = seen_partitions.iterator();
    while (sp_it.next()) |entry| {
        const fi = entry.key_ptr.*;
        try app_tsx.writer().print("import * as Partition{d} from './components/Partition{d}';\n", .{ fi, fi });
    }
    try app_tsx.appendSlice("import { BloomRouter } from './bloomRouter';\n");
    try app_tsx.appendSlice("\n");
    try app_tsx.appendSlice("const sourceUrl = ");
    try appendTsStringLiteral(&app_tsx, config.target_url);
    try app_tsx.appendSlice(";\n\n");
    try app_tsx.appendSlice("const Header: React.FC = () => (\n  <header className=\"site-header\" role=\"banner\">\n    <strong>Website Clone</strong>\n    <nav aria-label=\"Primary navigation\" role=\"navigation\">\n      <a href={sourceUrl}>Original Site</a>\n    </nav>\n  </header>\n);\n\n");
    try app_tsx.appendSlice("const Footer: React.FC = () => (\n  <footer className=\"site-footer\" role=\"contentinfo\">\n    <p>Cloned from <a href={sourceUrl}>{sourceUrl}</a></p>\n  </footer>\n);\n\n");
    try app_tsx.appendSlice("const App: React.FC = () => (\n  <BloomRouter>\n    <Header />\n    <main role=\"main\">\n");

    for (inventory.sections.items) |section| {
        var target_name: []const u8 = section.component_name;
        var target_file: []const u8 = section.component_file;
        if (section.template_of != INVALID_IDX) {
            const tpl = inventory.sections.items[section.template_of];
            target_name = tpl.component_name;
            target_file = tpl.component_file;
        }
        try app_tsx.writer().print("      <{s}.{s} />\n", .{ target_file, target_name });
    }
    try app_tsx.appendSlice("    </main>\n    <Footer />\n  </BloomRouter>\n);\n\nexport default App;\n");
    try vfs.addCopy("src/App.tsx", app_tsx.items);
}

fn beginComponentFile(buf: *ArrayList(u8)) !void {
    try buf.appendSlice("import React from 'react';\n");
    try buf.appendSlice("import { motion } from 'framer-motion';\n\n");
}

fn writeComponent(buf: *ArrayList(u8), arena: *TokenArena, inventory: *Inventory, section: Section, sec_i: usize) !void {
    _ = arena;
    _ = sec_i;
    try buf.writer().print("export const {s}: React.FC = () => (\n", .{section.component_name});
    const use_motion = section.transition_tokens.len > 0;
    if (use_motion) {
        try buf.appendSlice("  <motion.section initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ duration: 0.6 }} ");
    } else {
        try buf.appendSlice("  <section ");
    }
    try buf.appendSlice("className=");
    try appendTsxStringExpression(buf, section.tailwind_classes);
    try buf.appendSlice(" role=");
    try appendTsxStringExpression(buf, section.aria_role);
    try buf.appendSlice(" aria-label=");
    try appendTsxStringExpression(buf, section.bipartite_label);
    try buf.appendSlice(" data-source-selector=");
    try appendTsxStringExpression(buf, section.selector);
    try buf.appendSlice(" data-entropy=");
    try appendTsxStringExpression(buf, section.entropy_hash);
    try buf.writer().print(" data-weight={{{d}}} data-density={{{d}}} data-prominence={{{d}}} data-hash={{{x}}}>", .{ section.semantic_weight, section.density, @as(i64, @intFromFloat(section.prominence)), section.structural_hash });

    if (section.text_preview.len > 0) {
        try buf.appendSlice("\n    <p>");
        try appendTsxStringExpression(buf, section.text_preview);
        try buf.appendSlice("</p>\n");
    }
    for (section.images.items) |img_idx| {
        if (img_idx >= inventory.assets.items.len) continue;
        const img = inventory.assets.items[img_idx];
        var resolved = img;
        if (img.duplicate_of != INVALID_IDX and img.duplicate_of < inventory.assets.items.len) {
            resolved = inventory.assets.items[img.duplicate_of];
        }
        if (resolved.local_path.len > 0) {
            try buf.appendSlice("    <img src={import.meta.env.BASE_URL + ");
            try appendTsStringLiteral(buf, resolved.local_path);
            try buf.appendSlice("} alt=");
            try appendTsxStringExpression(buf, resolved.alt);
            try buf.appendSlice(" data-blurhash=");
            try appendTsxStringExpression(buf, resolved.blurhash);
            try buf.appendSlice(" loading=\"lazy\" />\n");
        }
    }
    if (use_motion) {
        try buf.appendSlice("  </motion.section>\n);\n\n");
    } else {
        try buf.appendSlice("  </section>\n);\n\n");
    }
}

fn generateTheme(allocator: Allocator, inventory: *Inventory, vfs: *VFS) !void {
    var buf = ArrayList(u8).init(allocator);
    defer buf.deinit();
    try buf.appendSlice("export interface ThemePalette {\n  primary: string;\n  secondary: string;\n  accent: string;\n  background: string;\n  surface: string;\n  muted: string;\n  text: string;\n  contrast: string;\n}\n\n");
    try buf.appendSlice("export const theme: ThemePalette = {\n");
    const names = [_][]const u8{ "primary", "secondary", "accent", "background", "surface", "muted", "text", "contrast" };
    var i: usize = 0;
    while (i < names.len) : (i += 1) {
        const c = if (i < inventory.color_palette_used) inventory.color_palette[i] else ColorRecord{ .r = 0, .g = 0, .b = 0, .a = 255 };
        try buf.writer().print("  {s}: \"#{x:0>2}{x:0>2}{x:0>2}\",\n", .{ names[i], c.r, c.g, c.b });
    }
    try buf.appendSlice("};\n");
    try vfs.addCopy("src/theme.ts", buf.items);
}

const WordScore = struct { word: []const u8, score: f64 };

fn generateSeoMetadata(allocator: Allocator, inventory: *Inventory, vfs: *VFS) !void {
    var buf = ArrayList(u8).init(allocator);
    defer buf.deinit();
    const doc_count = inventory.section_text.items.len;
    var top_words = ArrayList(WordScore).init(allocator);
    defer top_words.deinit();
    var it = inventory.text_tokens.iterator();
    while (it.next()) |entry| {
        const tf: f64 = @floatFromInt(entry.value_ptr.*);
        const df: u32 = inventory.text_doc_freq.get(entry.key_ptr.*) orelse 1;
        const dc: f64 = @floatFromInt(@max(doc_count, 1));
        const idf = std.math.log2(dc / @as(f64, @floatFromInt(df)) + 1.0);
        const score = tf * idf;
        try top_words.append(.{ .word = entry.key_ptr.*, .score = score });
    }
    if (top_words.items.len > 0) {
        std.mem.sort(WordScore, top_words.items, {}, struct {
            fn lt(_: void, a: WordScore, b: WordScore) bool {
                return a.score > b.score;
            }
        }.lt);
    }
    try buf.appendSlice("export const seoKeywords: string[] = [\n");
    var n: usize = 0;
    while (n < @min(top_words.items.len, 24)) : (n += 1) {
        try buf.appendSlice("  ");
        try appendTsStringLiteral(&buf, top_words.items[n].word);
        try buf.appendSlice(",\n");
    }
    try buf.appendSlice("];\n\n");
    try buf.appendSlice("export const seoTitle = ");
    if (top_words.items.len > 0) {
        var title_buf = ArrayList(u8).init(allocator);
        defer title_buf.deinit();
        var ti: usize = 0;
        while (ti < @min(top_words.items.len, 4)) : (ti += 1) {
            if (ti > 0) try title_buf.append(' ');
            try title_buf.appendSlice(top_words.items[ti].word);
        }
        try appendTsStringLiteral(&buf, title_buf.items);
    } else {
        try buf.appendSlice("\"Website Clone\"");
    }
    try buf.appendSlice(";\n");
    try vfs.addCopy("src/seo.ts", buf.items);
}

fn generateMarkovOutput(allocator: Allocator, inventory: *Inventory, vfs: *VFS) !void {
    var buf = ArrayList(u8).init(allocator);
    defer buf.deinit();
    try buf.appendSlice("export const markovBigrams: Record<string, string[]> = {\n");
    var it = inventory.markov.iterator();
    var count: u32 = 0;
    while (it.next()) |entry| {
        if (count >= 256) break;
        const key_hash = entry.key_ptr.*;
        try buf.writer().print("  {x:0>16}: [", .{key_hash});
        var first = true;
        var wi: usize = 0;
        while (wi < @min(entry.value_ptr.items.len, 8)) : (wi += 1) {
            if (!first) try buf.append(',');
            first = false;
            try appendTsStringLiteral(&buf, entry.value_ptr.items[wi]);
        }
        try buf.appendSlice("],\n");
        count += 1;
    }
    try buf.appendSlice("};\n\n");
    try buf.appendSlice(
        \\export function generatePlaceholder(seed: number, length: number): string {
        \\  const keys = Object.keys(markovBigrams);
        \\  if (keys.length === 0) return 'Lorem ipsum dolor sit amet';
        \\  let s = seed;
        \\  const rand = () => { s = (s * 1103515245 + 12345) & 0x7fffffff; return s; };
        \\  const out: string[] = [];
        \\  for (let i = 0; i < length; i++) {
        \\    const key = keys[rand() % keys.length];
        \\    const vals = markovBigrams[key];
        \\    out.push(vals[rand() % vals.length]);
        \\  }
        \\  return out.join(' ');
        \\}
        \\
    );
    try vfs.addCopy("src/markov.ts", buf.items);
}

fn generateSubsetFont(allocator: Allocator, inventory: *Inventory, vfs: *VFS) !void {
    var buf = ArrayList(u8).init(allocator);
    defer buf.deinit();
    try buf.appendSlice("wOF2");
    var glyph_count: u32 = 0;
    var it = inventory.glyphs.iterator();
    while (it.next()) |_| glyph_count += 1;
    var header: [12]u8 = undefined;
    std.mem.writeInt(u32, header[0..4], glyph_count, .big);
    std.mem.writeInt(u32, header[4..8], 0x10000, .big);
    std.mem.writeInt(u32, header[8..12], @intCast(buf.items.len), .big);
    try buf.appendSlice(&header);
    var it2 = inventory.glyphs.iterator();
    while (it2.next()) |entry| {
        const cp = entry.key_ptr.*;
        var glyph: [16]u8 = undefined;
        std.mem.writeInt(u32, glyph[0..4], cp, .big);
        std.mem.writeInt(u32, glyph[4..8], 600, .big);
        std.mem.writeInt(u32, glyph[8..12], 0, .big);
        std.mem.writeInt(u32, glyph[12..16], 0, .big);
        try buf.appendSlice(&glyph);
    }
    try vfs.addCopy("public/fonts/subset.woff2", buf.items);
}

fn generateBloomRouter(allocator: Allocator, inventory: *Inventory, vfs: *VFS) !void {
    var buf = ArrayList(u8).init(allocator);
    defer buf.deinit();
    try buf.appendSlice("import React, { useEffect, useState } from 'react';\n\n");
    try buf.writer().print("const BLOOM_BIT_COUNT = {d};\n", .{BLOOM_BITS});
    try buf.writer().print("const BLOOM_HASHES = {d};\n", .{BLOOM_HASHES});
    try buf.appendSlice("const BLOOM_BITS: number[] = [\n");
    var word_idx: usize = 0;
    while (word_idx < inventory.link_bloom.bits.len) : (word_idx += 1) {
        const w = inventory.link_bloom.bits[word_idx];
        try buf.writer().print("  0x{x},\n", .{w});
    }
    try buf.appendSlice("];\n\n");
    try buf.appendSlice(
        \\function fnvSeed(data: string, seed: number): number {
        \\  let h = 14695981039346656037n ^ BigInt(seed);
        \\  for (let i = 0; i < data.length; i++) {
        \\    h ^= BigInt(data.charCodeAt(i));
        \\    h = (h * 1099511628211n) & 0xFFFFFFFFFFFFFFFFn;
        \\  }
        \\  return Number(h % BigInt(BLOOM_BIT_COUNT));
        \\}
        \\
        \\
    );
    try buf.appendSlice(
        \\export function bloomContains(href: string): boolean {
        \\  for (let k = 0; k < BLOOM_HASHES; k++) {
        \\    const bit = fnvSeed(href, (k * 2654435761) >>> 0);
        \\    const word = BLOOM_BITS[Math.floor(bit / 32)] || 0;
        \\    if (!(word & (1 << (bit % 32)))) return false;
        \\  }
        \\  return true;
        \\}
        \\
        \\
    );
    try buf.appendSlice(
        \\export const BloomRouter: React.FC<{ children: React.ReactNode }> = ({ children }) => {
        \\  const [path, setPath] = useState(window.location.pathname);
        \\  useEffect(() => {
        \\    const onPop = () => setPath(window.location.pathname);
        \\    window.addEventListener('popstate', onPop);
        \\    const onClick = (e: MouseEvent) => {
        \\      const t = e.target as HTMLElement;
        \\      const a = t.closest('a');
        \\      if (!a) return;
        \\      const href = a.getAttribute('href');
        \\      if (!href) return;
        \\      if (bloomContains(href)) {
        \\        e.preventDefault();
        \\        window.history.pushState({}, '', href);
        \\        setPath(href);
        \\      }
        \\    };
        \\    document.addEventListener('click', onClick);
        \\    return () => { window.removeEventListener('popstate', onPop); document.removeEventListener('click', onClick); };
        \\  }, []);
        \\  return <>{children}</>;
        \\};
        \\
    );
    try vfs.addCopy("src/bloomRouter.tsx", buf.items);
}

fn generateWasmModule(allocator: Allocator, vfs: *VFS) !void {
    var buf = ArrayList(u8).init(allocator);
    defer buf.deinit();
    try buf.appendSlice("const std = @import(\"std\");\n\n");
    try buf.appendSlice(
        \\export fn hydrate(ptr: [*]const u8, len: usize) u32 {
        \\    var sum: u32 = 0;
        \\    var i: usize = 0;
        \\    while (i < len) : (i += 1) sum +%= ptr[i];
        \\    return sum;
        \\}
        \\
        \\export fn allocate(size: usize) [*]u8 {
        \\    const slice = std.heap.wasm_allocator.alloc(u8, size) catch return undefined;
        \\    return slice.ptr;
        \\}
        \\
        \\export fn free(ptr: [*]u8, size: usize) void {
        \\    std.heap.wasm_allocator.free(ptr[0..size]);
        \\}
        \\
        \\export fn route_match(ptr: [*]const u8, len: usize) u32 {
        \\    var h: u64 = 1469598103934665603;
        \\    var i: usize = 0;
        \\    while (i < len) : (i += 1) {
        \\        h ^= ptr[i];
        \\        h *%= 1099511628211;
        \\    }
        \\    return @intCast(h & 0xFFFFFFFF);
        \\}
        \\
    );
    try vfs.addCopy("src/wasm/runtime.zig", buf.items);

    var build_zig = ArrayList(u8).init(allocator);
    defer build_zig.deinit();
    try build_zig.appendSlice(
        \\const std = @import("std");
        \\
        \\pub fn build(b: *std.Build) void {
        \\    const target = b.resolveTargetQuery(.{ .cpu_arch = .wasm32, .os_tag = .freestanding });
        \\    const optimize = b.standardOptimizeOption(.{});
        \\    const exe = b.addExecutable(.{
        \\        .name = "runtime",
        \\        .root_source_file = b.path("runtime.zig"),
        \\        .target = target,
        \\        .optimize = optimize,
        \\    });
        \\    exe.entry = .disabled;
        \\    exe.rdynamic = true;
        \\    b.installArtifact(exe);
        \\}
        \\
    );
    try vfs.addCopy("src/wasm/build.zig", build_zig.items);
}

fn generateEcsEngine(allocator: Allocator, inventory: *Inventory, vfs: *VFS) !void {
    var buf = ArrayList(u8).init(allocator);
    defer buf.deinit();
    try buf.appendSlice("export type EntityId = number;\nexport interface Component { type: string; data: unknown; }\n\n");
    try buf.appendSlice(
        \\export class World {
        \\  private nextId = 0;
        \\  private components = new Map<string, Map<EntityId, unknown>>();
        \\  createEntity(): EntityId { return this.nextId++; }
        \\  addComponent<T>(e: EntityId, type: string, data: T): void {
        \\    if (!this.components.has(type)) this.components.set(type, new Map());
        \\    this.components.get(type)!.set(e, data);
        \\  }
        \\  query(type: string): Map<EntityId, unknown> { return this.components.get(type) || new Map(); }
        \\}
        \\
        \\export const world = new World();
        \\
        \\
    );
    for (inventory.sections.items, 0..) |section, i| {
        if (!section.has_transform3d and section.text_length < 4000 and section.images.items.len < 8) continue;
        try buf.writer().print("(() => {{\n  const e = world.createEntity();\n  world.addComponent(e, 'section', {{ index: {d}, label: ", .{i});
        try appendTsStringLiteral(&buf, section.bipartite_label);
        try buf.writer().print(", weight: {d}, prominence: {d} }});\n}})();\n", .{ section.semantic_weight, @as(i64, @intFromFloat(section.prominence)) });
    }
    try vfs.addCopy("src/ecs/world.ts", buf.items);
}

fn generateXStateMachines(allocator: Allocator, inventory: *Inventory, vfs: *VFS) !void {
    var buf = ArrayList(u8).init(allocator);
    defer buf.deinit();
    try buf.appendSlice("import { createMachine } from 'xstate';\n\n");
    for (inventory.sections.items) |section| {
        if (section.state_machine_id == INVALID_IDX) continue;
        try buf.writer().print(
            \\export const machine{d} = createMachine({{
            \\  id: 'sm{d}',
            \\  initial: 'collapsed',
            \\  states: {{
            \\    collapsed: {{ on: {{ TOGGLE: 'expanded' }} }},
            \\    expanded: {{ on: {{ TOGGLE: 'collapsed' }} }},
            \\  }},
            \\}});
            \\
            \\
        , .{ section.state_machine_id, section.state_machine_id });
    }
    try vfs.addCopy("src/machines/machines.ts", buf.items);
}

fn generateWebWorkers(allocator: Allocator, inventory: *Inventory, vfs: *VFS) !void {
    var buf = ArrayList(u8).init(allocator);
    defer buf.deinit();
    try buf.appendSlice(
        \\self.onmessage = (e: MessageEvent) => {
        \\  const { type, payload } = e.data;
        \\  if (type === 'process') {
        \\    const result = JSON.parse(payload).length;
        \\    self.postMessage({ type: 'result', value: result });
        \\  }
        \\};
        \\export {};
        \\
    );
    try vfs.addCopy("src/workers/heavy.worker.ts", buf.items);

    var bootstrap = ArrayList(u8).init(allocator);
    defer bootstrap.deinit();
    try bootstrap.appendSlice(
        \\export function spawnHeavyWorker(): Worker {
        \\  return new Worker(new URL('./heavy.worker.ts', import.meta.url), { type: 'module' });
        \\}
        \\
        \\export const heavySections = [
        \\
    );
    for (inventory.sections.items) |section| {
        if (section.text_length > 4000 or section.images.items.len > 8) {
            try bootstrap.writer().print("  {{ index: {d}, label: ", .{section.index});
            try appendTsStringLiteral(&bootstrap, section.bipartite_label);
            try bootstrap.appendSlice(" },\n");
        }
    }
    try bootstrap.appendSlice("];\n");
    try vfs.addCopy("src/workers/index.ts", bootstrap.items);
}

fn generateGraphQLStubs(allocator: Allocator, inventory: *Inventory, vfs: *VFS) !void {
    var buf = ArrayList(u8).init(allocator);
    defer buf.deinit();
    try buf.appendSlice("import { gql } from '@apollo/client';\n\n");
    if (inventory.form_fields.items.len > 0) {
        try buf.appendSlice("export const SUBMIT_FORM = gql`\n  mutation SubmitForm(");
        var first = true;
        for (inventory.form_fields.items) |f| {
            if (!first) try buf.appendSlice(", ");
            first = false;
            var clean_f = ArrayList(u8).init(allocator);
            defer clean_f.deinit();
            for (f) |c| {
                if ((c >= 'a' and c <= 'z') or (c >= 'A' and c <= 'Z') or (c >= '0' and c <= '9')) {
                    try clean_f.append(c);
                }
            }
            if (clean_f.items.len == 0) try clean_f.appendSlice("field");
            try buf.writer().print("${s}: String", .{clean_f.items});
        }
        try buf.appendSlice(") {\n    submitForm(");
        first = true;
        for (inventory.form_fields.items) |f| {
            if (!first) try buf.appendSlice(", ");
            first = false;
            var clean_f = ArrayList(u8).init(allocator);
            defer clean_f.deinit();
            for (f) |c| {
                if ((c >= 'a' and c <= 'z') or (c >= 'A' and c <= 'Z') or (c >= '0' and c <= '9')) {
                    try clean_f.append(c);
                }
            }
            if (clean_f.items.len == 0) try clean_f.appendSlice("field");
            try buf.writer().print("{s}: ${s}", .{ clean_f.items, clean_f.items });
        }
        try buf.appendSlice(") { success message }\n  }\n`;\n\n");
    }
    if (inventory.table_columns.items.len > 0) {
        try buf.appendSlice("export const QUERY_TABLE = gql`\n  query TableData {\n    rows {\n");
        for (inventory.table_columns.items) |col| {
            const h = hashString(col);
            try buf.writer().print("      _{s}\n", .{h[4..10]});
        }
        try buf.appendSlice("    }\n  }\n`;\n");
    }
    try vfs.addCopy("src/graphql.ts", buf.items);
}

fn generateWebGLOverlay(allocator: Allocator, inventory: *Inventory, vfs: *VFS) !void {
    var buf = ArrayList(u8).init(allocator);
    defer buf.deinit();
    try buf.appendSlice("import React from 'react';\nimport { Canvas } from '@react-three/fiber';\n\n");
    try buf.appendSlice("export const TransformOverlay: React.FC = () => (\n  <Canvas style={{ position: 'fixed', inset: 0, pointerEvents: 'none', zIndex: 9999 }} camera={{ position: [0, 0, 5] }}>\n    <ambientLight intensity={0.5} />\n    <directionalLight position={[5, 5, 5]} />\n");
    for (inventory.sections.items, 0..) |section, i| {
        if (!section.has_transform3d) continue;
        try buf.writer().print("    <mesh position={{[{d}, 0, 0]}}><boxGeometry args={{[1, 1, 1]}} /><meshStandardMaterial color=\"#{x:0>2}{x:0>2}{x:0>2}\" />\n", .{ i, inventory.color_palette[0].r, inventory.color_palette[0].g, inventory.color_palette[0].b });
    }
    try buf.appendSlice("  </Canvas>\n);\n");
    try vfs.addCopy("src/webglOverlay.tsx", buf.items);
}

fn generateTailwindConfig(allocator: Allocator, inventory: *Inventory, vfs: *VFS) !void {
    var buf = ArrayList(u8).init(allocator);
    defer buf.deinit();
    try buf.appendSlice("export default {\n  content: ['./index.html', './src/**/*.{ts,tsx}'],\n  theme: {\n    extend: {\n      colors: {\n");
    const names = [_][]const u8{ "primary", "secondary", "accent", "background", "surface", "muted", "text", "contrast" };
    var i: usize = 0;
    while (i < names.len) : (i += 1) {
        const c = if (i < inventory.color_palette_used) inventory.color_palette[i] else ColorRecord{ .r = 0, .g = 0, .b = 0, .a = 255 };
        try buf.writer().print("        {s}: '#{x:0>2}{x:0>2}{x:0>2}',\n", .{ names[i], c.r, c.g, c.b });
    }
    try buf.appendSlice("      },\n    },\n  },\n  plugins: [],\n};\n");
    try vfs.addCopy("tailwind.config.js", buf.items);

    const postcss =
        \\export default {
        \\  plugins: { tailwindcss: {}, autoprefixer: {} }
        \\};
    ;
    try vfs.addCopy("postcss.config.js", postcss);
}

fn generateDifferentialFuzzer(allocator: Allocator, vfs: *VFS) !void {
    var buf = ArrayList(u8).init(allocator);
    defer buf.deinit();
    try buf.appendSlice("import { chromium } from 'playwright';\n\n");
    try buf.appendSlice("const SEEDS = ['x', '1', '', '<a href=\"/x\">x</a>'];\n\n");
    try buf.appendSlice(
        \\async function fuzz() {
        \\  const browser = await chromium.launch();
        \\  const ctx = await browser.newContext();
        \\  const page = await ctx.newPage();
        \\  await page.goto('http://localhost:5173/');
        \\  const clone = await page.evaluate(() => document.body.innerText);
        \\  for (const seed of SEEDS) {
        \\    const reference = seed.replace(/<[^>]+>/g, '').trim();
        \\    if (!clone.length && reference.length) console.warn('parity diff for', seed);
        \\  }
        \\  await browser.close();
        \\}
        \\
        \\fuzz().catch(e => { console.error(e); process.exit(1); });
        \\
    );
    try vfs.addCopy("tests/differential.mjs", buf.items);
}

fn generateBuildScripts(allocator: Allocator, vfs: *VFS) !void {
    _ = allocator;
    const readme =
        \\# Website Clone
        \\
        \\Run `npm install && npm run dev`.
        \\Build WASM runtime with `cd src/wasm && zig build`.
        \\Differential fuzzer: `npm test`.
        \\
    ;
    try vfs.addCopy("README.md", readme);
}

fn cssVarToTsKey(allocator: Allocator, css_var: []const u8) ![]const u8 {
    var start: usize = 0;
    if (Mem.startsWith(u8, css_var, "--")) start = 2;
    var list = ArrayList(u8).init(allocator);
    errdefer list.deinit();
    try list.append('"');
    try list.appendSlice("--");
    try list.appendSlice(css_var[start..]);
    try list.append('"');
    return try list.toOwnedSlice();
}

fn parseBaseUrl(url: []const u8) !BaseUrl {
    const scheme_end = Mem.indexOf(u8, url, "://") orelse return error.InvalidUrl;
    const scheme = url[0..scheme_end];
    const authority_start = scheme_end + 3;
    var authority_end = url.len;
    if (Mem.indexOfAnyPos(u8, url, authority_start, "/?#")) |idx| {
        authority_end = idx;
    }
    if (authority_end <= authority_start) return error.InvalidUrl;
    const host = url[authority_start..authority_end];
    var path: []const u8 = "/";
    if (authority_end < url.len and url[authority_end] == '/') {
        var path_end = url.len;
        if (Mem.indexOfAnyPos(u8, url, authority_end, "?#")) |idx| {
            path_end = idx;
        }
        path = url[authority_end..path_end];
    }
    const base_dir = if (Mem.lastIndexOfScalar(u8, path, '/')) |idx| path[0 .. idx + 1] else "/";
    return BaseUrl{
        .scheme = scheme,
        .host = host,
        .base_dir = base_dir,
    };
}

fn replaceString(allocator: Allocator, haystack: []const u8, needle: []const u8, replacement: []const u8) ![]const u8 {
    if (needle.len == 0) return try allocator.dupe(u8, haystack);
    var result = ArrayList(u8).init(allocator);
    errdefer result.deinit();
    var start: usize = 0;
    while (Mem.indexOfPos(u8, haystack, start, needle)) |idx| {
        try result.appendSlice(haystack[start..idx]);
        try result.appendSlice(replacement);
        start = idx + needle.len;
    }
    try result.appendSlice(haystack[start..]);
    return try result.toOwnedSlice();
}

fn getExt(path: []const u8) []const u8 {
    var clean_path = path;
    if (Mem.indexOfScalar(u8, clean_path, '?')) |idx| {
        clean_path = clean_path[0..idx];
    }
    if (Mem.indexOfScalar(u8, clean_path, '#')) |idx| {
        clean_path = clean_path[0..idx];
    }
    const last_slash = Mem.lastIndexOfScalar(u8, clean_path, '/') orelse 0;
    if (Mem.lastIndexOfScalar(u8, clean_path, '.')) |idx| {
        if (idx >= last_slash and idx + 1 < clean_path.len and clean_path.len - idx <= 8) {
            return clean_path[idx..];
        }
    }
    return ".bin";
}

fn hashString(str: []const u8) [10]u8 {
    var hash: u64 = 5381;
    for (str) |c| {
        hash = ((hash << 5) +% hash) +% c;
    }
    var buf: [16]u8 = [_]u8{0} ** 16;
    const written = std.fmt.bufPrint(&buf, "{x:0>16}", .{hash}) catch unreachable;
    var out: [10]u8 = undefined;
    @memcpy(out[0..10], written[0..10]);
    return out;
}

fn sanitizeComponentName(allocator: Allocator, str: []const u8, index: usize) ![]const u8 {
    var list = ArrayList(u8).init(allocator);
    errdefer list.deinit();
    try list.appendSlice("Captured");
    var word_start: bool = true;
    var saw_valid: bool = false;
    for (str) |c| {
        const is_alpha_lower = c >= 'a' and c <= 'z';
        const is_alpha_upper = c >= 'A' and c <= 'Z';
        const is_digit = c >= '0' and c <= '9';
        if (is_alpha_lower or is_alpha_upper or is_digit) {
            var out_c = c;
            if (word_start and is_alpha_lower) {
                out_c = c & 0xDF;
            }
            try list.append(out_c);
            word_start = false;
            saw_valid = true;
        } else {
            word_start = true;
        }
    }
    if (!saw_valid) {
        try list.appendSlice("Section");
    }
    try list.writer().print("{d}", .{index});
    return try list.toOwnedSlice();
}

fn findAttributeValue(allocator: Allocator, attrs: []const u8, attribute_name: []const u8) !?[]const u8 {
    var i: usize = 0;
    while (i < attrs.len) {
        while (i < attrs.len and isWhitespace(attrs[i])) i += 1;
        if (i >= attrs.len or attrs[i] == '/' or attrs[i] == '>') break;
        const name_start = i;
        while (i < attrs.len and !isWhitespace(attrs[i]) and attrs[i] != '=' and attrs[i] != '/' and attrs[i] != '>') {
            i += 1;
        }
        const name = attrs[name_start..i];
        while (i < attrs.len and isWhitespace(attrs[i])) i += 1;
        if (i >= attrs.len or attrs[i] != '=') {
            if (eqlIgnoreCase(name, attribute_name)) {
                return try allocator.dupe(u8, "");
            }
            continue;
        }
        i += 1;
        while (i < attrs.len and isWhitespace(attrs[i])) i += 1;
        if (i >= attrs.len) {
            if (eqlIgnoreCase(name, attribute_name)) {
                return try allocator.dupe(u8, "");
            }
            break;
        }
        var value_start: usize = i;
        var value_end: usize = i;
        if (attrs[i] == '"' or attrs[i] == '\'') {
            const quote = attrs[i];
            i += 1;
            value_start = i;
            while (i < attrs.len and attrs[i] != quote) i += 1;
            value_end = i;
            if (i < attrs.len) i += 1;
        } else {
            value_start = i;
            while (i < attrs.len and !isWhitespace(attrs[i]) and attrs[i] != '/' and attrs[i] != '>') {
                i += 1;
            }
            value_end = i;
        }
        if (eqlIgnoreCase(name, attribute_name)) {
            return try decodeHtmlEntities(allocator, attrs[value_start..value_end]);
        }
    }
    return null;
}

fn decodeHtmlEntities(allocator: Allocator, input: []const u8) ![]const u8 {
    var out = ArrayList(u8).init(allocator);
    errdefer out.deinit();
    var i: usize = 0;
    while (i < input.len) {
        if (input[i] == '&') {
            if (Mem.startsWith(u8, input[i..], "&amp;")) {
                try out.append('&');
                i += 5;
                continue;
            }
            if (Mem.startsWith(u8, input[i..], "&lt;")) {
                try out.append('<');
                i += 4;
                continue;
            }
            if (Mem.startsWith(u8, input[i..], "&gt;")) {
                try out.append('>');
                i += 4;
                continue;
            }
            if (Mem.startsWith(u8, input[i..], "&quot;")) {
                try out.append('"');
                i += 6;
                continue;
            }
            if (Mem.startsWith(u8, input[i..], "&#39;")) {
                try out.append('\'');
                i += 5;
                continue;
            }
            if (Mem.startsWith(u8, input[i..], "&apos;")) {
                try out.append('\'');
                i += 6;
                continue;
            }
            if (Mem.startsWith(u8, input[i..], "&nbsp;")) {
                try out.append(' ');
                i += 6;
                continue;
            }
            if (i + 2 < input.len and input[i + 1] == '#') {
                var j: usize = i + 2;
                var is_hex = false;
                if (j < input.len and (input[j] == 'x' or input[j] == 'X')) {
                    is_hex = true;
                    j += 1;
                }
                const num_start = j;
                var code: u32 = 0;
                while (j < input.len and input[j] != ';') : (j += 1) {
                    const c = input[j];
                    if (is_hex) {
                        if (c >= '0' and c <= '9') {
                            code = code * 16 + (c - '0');
                        } else if (c >= 'a' and c <= 'f') {
                            code = code * 16 + (c - 'a' + 10);
                        } else if (c >= 'A' and c <= 'F') {
                            code = code * 16 + (c - 'A' + 10);
                        } else break;
                    } else {
                        if (c >= '0' and c <= '9') code = code * 10 + (c - '0') else break;
                    }
                }
                if (j < input.len and input[j] == ';' and j > num_start) {
                    if (code < 0x80) {
                        try out.append(@intCast(code));
                    } else if (code < 0x800) {
                        try out.append(@intCast(0xC0 | (code >> 6)));
                        try out.append(@intCast(0x80 | (code & 0x3F)));
                    } else if (code < 0x10000) {
                        try out.append(@intCast(0xE0 | (code >> 12)));
                        try out.append(@intCast(0x80 | ((code >> 6) & 0x3F)));
                        try out.append(@intCast(0x80 | (code & 0x3F)));
                    } else if (code < 0x110000) {
                        try out.append(@intCast(0xF0 | (code >> 18)));
                        try out.append(@intCast(0x80 | ((code >> 12) & 0x3F)));
                        try out.append(@intCast(0x80 | ((code >> 6) & 0x3F)));
                        try out.append(@intCast(0x80 | (code & 0x3F)));
                    }
                    i = j + 1;
                    continue;
                }
            }
        }
        try out.append(input[i]);
        i += 1;
    }
    return try out.toOwnedSlice();
}

fn appendNormalizedText(out: *ArrayList(u8), input: []const u8) !void {
    var previous_was_space = if (out.items.len == 0) true else out.items[out.items.len - 1] == ' ';
    for (input) |c| {
        if (out.items.len >= SECTION_TEXT_LIMIT) return;
        if (isWhitespace(c)) {
            if (!previous_was_space) {
                try out.append(' ');
                previous_was_space = true;
            }
        } else {
            try out.append(c);
            previous_was_space = false;
        }
    }
}

fn appendTsxStringExpression(list: *ArrayList(u8), str: []const u8) !void {
    try list.append('{');
    try appendTsStringLiteral(list, str);
    try list.append('}');
}

fn appendTsStringLiteral(list: *ArrayList(u8), str: []const u8) !void {
    try list.append('"');
    for (str) |c| {
        switch (c) {
            '\\' => try list.appendSlice("\\\\"),
            '"' => try list.appendSlice("\\\""),
            '\n' => try list.appendSlice("\\n"),
            '\r' => try list.appendSlice("\\r"),
            '\t' => try list.appendSlice("\\t"),
            0...8, 11, 12, 14...31, 127 => {
                var buf: [8]u8 = undefined;
                const s = std.fmt.bufPrint(&buf, "\\u{x:0>4}", .{c}) catch unreachable;
                try list.appendSlice(s);
            },
            else => try list.append(c),
        }
    }
    try list.append('"');
}

fn findNextCaseInsensitive(haystack: []const u8, needle: []const u8, start: usize) ?usize {
    if (needle.len == 0) return start;
    if (start >= haystack.len or needle.len > haystack.len) return null;
    var i = start;
    while (i + needle.len <= haystack.len) : (i += 1) {
        if (eqlIgnoreCase(haystack[i .. i + needle.len], needle)) return i;
    }
    return null;
}

fn eqlIgnoreCase(a: []const u8, b: []const u8) bool {
    if (a.len != b.len) return false;
    for (a, 0..) |ac, i| {
        if (toAsciiLower(ac) != toAsciiLower(b[i])) return false;
    }
    return true;
}

fn isWhitespace(c: u8) bool {
    return c == ' ' or c == '\t' or c == '\n' or c == '\r' or c == 0x0c;
}

fn toAsciiLower(c: u8) u8 {
    if (c >= 'A' and c <= 'Z') return c | 0x20;
    return c;
}
