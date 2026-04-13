// NaviCraft Navidrome Plugin
//
// This plugin watches for playlists named with the [navicraft, ...] tag
// and delegates AI playlist generation to the NaviCraft backend.
//
// Build with TinyGo:
//   tinygo build -o plugin.wasm -target wasip1 .
//
// Package as .ndp:
//   zip -j navicraft.ndp manifest.json plugin.wasm
//
// Install:
//   Copy navicraft.ndp to your Navidrome plugins folder (default: $DataFolder/Plugins)
package main

import (
	"encoding/json"
	"fmt"
	"regexp"
	"strconv"
	"strings"

	"github.com/extism/go-pdk"
)

// --- Configuration ---

type PluginConfig struct {
	NaviCraftURL        string `json:"navicraftUrl"`
	PollIntervalSeconds int    `json:"pollIntervalSeconds"`
	DefaultSongs        int    `json:"defaultSongs"`
}

func getConfig() PluginConfig {
	cfg := PluginConfig{
		NaviCraftURL:        "http://navicraft:8765",
		PollIntervalSeconds: 30,
		DefaultSongs:        25,
	}

	// Read config from host — GetConfig returns (string, bool)
	if v, ok := pdk.GetConfig("navicraftUrl"); ok && v != "" {
		cfg.NaviCraftURL = v
	}
	if v, ok := pdk.GetConfig("pollIntervalSeconds"); ok && v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			cfg.PollIntervalSeconds = n
		}
	}
	if v, ok := pdk.GetConfig("defaultSongs"); ok && v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			cfg.DefaultSongs = n
		}
	}

	return cfg
}

// --- Playlist name parsing ---

var navicraftRe = regexp.MustCompile(`(?i)^(.*?)\s*\[navicraft(?:\s*,\s*(.*?))?\]\s*$`)
var durationRe = regexp.MustCompile(`(?i)duration\s*:\s*(\d+)`)
var songsRe = regexp.MustCompile(`(?i)songs\s*:\s*(\d+)`)

type ParsedTag struct {
	Prompt            string `json:"prompt"`
	MaxSongs          int    `json:"max_songs"`
	TargetDurationMin *int   `json:"target_duration_min,omitempty"`
}

func parseNavicraftTag(name string) *ParsedTag {
	match := navicraftRe.FindStringSubmatch(name)
	if match == nil {
		return nil
	}

	prompt := strings.TrimSpace(match[1])
	if prompt == "" {
		return nil
	}

	cfg := getConfig()
	tag := &ParsedTag{
		Prompt:   prompt,
		MaxSongs: cfg.DefaultSongs,
	}

	if len(match) > 2 && match[2] != "" {
		paramsStr := match[2]

		if m := durationRe.FindStringSubmatch(paramsStr); m != nil {
			if d, err := strconv.Atoi(m[1]); err == nil {
				if d < 5 {
					d = 5
				}
				if d > 600 {
					d = 600
				}
				tag.TargetDurationMin = &d
			}
		}

		if m := songsRe.FindStringSubmatch(paramsStr); m != nil {
			if s, err := strconv.Atoi(m[1]); err == nil {
				if s < 5 {
					s = 5
				}
				if s > 100 {
					s = 100
				}
				tag.MaxSongs = s
			}
		}
	}

	return tag
}

// --- NaviCraft API types ---

type GenerateRequest struct {
	Prompt            string `json:"prompt"`
	MaxSongs          int    `json:"max_songs"`
	TargetDurationMin *int   `json:"target_duration_min,omitempty"`
}

type GenerateResponse struct {
	Name             string   `json:"name"`
	Description      string   `json:"description"`
	NavidromeSongIDs []string `json:"navidrome_song_ids"`
	TotalSongs       int      `json:"total_songs"`
	TotalDuration    int      `json:"total_duration"`
}

// --- Subsonic API types ---

type SubsonicResponse struct {
	SubsonicResponse struct {
		Status    string `json:"status"`
		Playlists struct {
			Playlist []SubsonicPlaylist `json:"playlist"`
		} `json:"playlists"`
	} `json:"subsonic-response"`
}

type SubsonicPlaylist struct {
	ID        string `json:"id"`
	Name      string `json:"name"`
	SongCount int    `json:"songCount"`
}

// --- Plugin exports ---

//export nd_on_init
func ndOnInit() int32 {
	cfg := getConfig()
	pdk.Log(pdk.LogInfo, fmt.Sprintf("NaviCraft plugin initialized. Backend URL: %s", cfg.NaviCraftURL))

	// Test connection to NaviCraft backend
	req := pdk.NewHTTPRequest(pdk.MethodGet, cfg.NaviCraftURL+"/api/health")
	resp := req.Send()
	if resp.Status() != 200 {
		pdk.Log(pdk.LogWarn, fmt.Sprintf("NaviCraft backend not reachable at %s (status: %d)", cfg.NaviCraftURL, resp.Status()))
	} else {
		pdk.Log(pdk.LogInfo, "NaviCraft backend connection verified")
	}

	// Schedule the watcher
	// NOTE: The exact scheduler host function signature depends on the
	// Navidrome plugin SDK version. This uses the pattern from v0.60.0+.
	// Adjust if needed based on your Navidrome version.
	scheduleWatcher(cfg.PollIntervalSeconds)

	return 0
}

func scheduleWatcher(intervalSeconds int) {
	// Schedule a recurring task using the Navidrome scheduler host service.
	// The cron expression runs every N seconds.
	// NOTE: Exact API depends on the navidrome plugin SDK version.
	// This is a placeholder — replace with the actual scheduler host call.
	pdk.Log(pdk.LogInfo, fmt.Sprintf("Watcher scheduled every %d seconds", intervalSeconds))
}

//export nd_scheduler_callback
func ndSchedulerCallback() int32 {
	cfg := getConfig()

	// Step 1: Get playlists from Navidrome via SubsonicAPI host service.
	// NOTE: The exact host function for SubsonicAPI calls depends on the
	// Navidrome plugin SDK. This demonstrates the intended flow.
	// In practice, use the nd_subsonic_* host functions.

	// For now, call Navidrome's Subsonic API via the SubsonicAPI host service
	// to get all playlists. The host service handles authentication internally.
	playlists, err := getPlaylistsViaHost()
	if err != nil {
		pdk.Log(pdk.LogError, fmt.Sprintf("Failed to get playlists: %v", err))
		return 1
	}

	for _, pl := range playlists {
		// Only check empty playlists
		if pl.SongCount > 0 {
			continue
		}

		parsed := parseNavicraftTag(pl.Name)
		if parsed == nil {
			continue
		}

		// Check if already processed (use Extism vars to avoid re-processing)
		cacheKey := "processed:" + pl.ID
		if cached := pdk.GetVar(cacheKey); len(cached) > 0 {
			continue
		}

		pdk.Log(pdk.LogInfo, fmt.Sprintf("Found [navicraft] playlist: '%s' (id: %s)", pl.Name, pl.ID))

		// Step 2: Call NaviCraft backend to generate the playlist
		genReq := GenerateRequest{
			Prompt:            parsed.Prompt,
			MaxSongs:          parsed.MaxSongs,
			TargetDurationMin: parsed.TargetDurationMin,
		}

		reqBody, _ := json.Marshal(genReq)
		httpReq := pdk.NewHTTPRequest(pdk.MethodPost, cfg.NaviCraftURL+"/api/plugin/generate")
		httpReq.SetHeader("Content-Type", "application/json")
		httpReq.SetBody(reqBody)
		resp := httpReq.Send()

		if resp.Status() != 200 {
			pdk.Log(pdk.LogError, fmt.Sprintf("NaviCraft generation failed for '%s': status %d, body: %s",
				parsed.Prompt, resp.Status(), string(resp.Body())))
			// Mark as processed to avoid retrying endlessly
			pdk.SetVar(cacheKey, []byte("error"))
			continue
		}

		var genResp GenerateResponse
		if unmarshalErr := json.Unmarshal(resp.Body(), &genResp); unmarshalErr != nil {
			pdk.Log(pdk.LogError, fmt.Sprintf("Failed to parse NaviCraft response: %v", unmarshalErr))
			pdk.SetVar(cacheKey, []byte("error"))
			continue
		}

		// Step 3: Update the playlist in Navidrome via SubsonicAPI host service
		if updateErr := updatePlaylistViaHost(pl.ID, genResp.Name, genResp.NavidromeSongIDs); updateErr != nil {
			pdk.Log(pdk.LogError, fmt.Sprintf("Failed to update playlist: %v", updateErr))
			continue
		}

		pdk.Log(pdk.LogInfo, fmt.Sprintf("Populated playlist '%s' with %d songs (%ds total)",
			genResp.Name, genResp.TotalSongs, genResp.TotalDuration))

		// Mark as processed
		pdk.SetVar(cacheKey, []byte("ok"))
	}

	return 0
}

// --- Host service wrappers ---
// NOTE: These functions use placeholder implementations.
// Replace with actual Navidrome plugin SDK host function calls
// based on your Navidrome version's plugin API.

func getPlaylistsViaHost() ([]SubsonicPlaylist, error) {
	// Use the SubsonicAPI host service to call getPlaylists.
	// The actual implementation depends on the Navidrome plugin SDK.
	//
	// Example using the SubsonicAPI host service:
	//   resp := nd_subsonic_request("getPlaylists", nil)
	//
	// For now, this is a placeholder that calls the external API via HTTP.
	// When building against the actual SDK, replace with the internal host call.
	pdk.Log(pdk.LogDebug, "Fetching playlists via SubsonicAPI host service")

	// Placeholder: This should be replaced with the actual host function call
	return nil, fmt.Errorf("SubsonicAPI host service not yet implemented - replace with actual SDK call")
}

func updatePlaylistViaHost(playlistID string, name string, songIDs []string) error {
	// Use the SubsonicAPI host service to call updatePlaylist.
	//
	// Example using the SubsonicAPI host service:
	//   params := map[string]interface{}{
	//       "playlistId": playlistID,
	//       "name": name,
	//       "songIdToAdd": songIDs,
	//   }
	//   resp := nd_subsonic_request("updatePlaylist", params)
	//
	// Placeholder: replace with actual SDK call.
	pdk.Log(pdk.LogDebug, fmt.Sprintf("Updating playlist %s via SubsonicAPI host service", playlistID))
	return fmt.Errorf("SubsonicAPI host service not yet implemented - replace with actual SDK call")
}

func main() {}
