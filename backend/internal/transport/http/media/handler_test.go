package media

import (
	"bytes"
	"context"
	"encoding/base64"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"

	mediaapp "github.com/chenyme/grok2api/backend/internal/application/media"
	localmedia "github.com/chenyme/grok2api/backend/internal/infra/media"
	"github.com/chenyme/grok2api/backend/internal/infra/persistence/relational"
	"github.com/gin-gonic/gin"
)

func TestPublicImageSupportsGetHeadAndETag(t *testing.T) {
	gin.SetMode(gin.TestMode)
	ctx := context.Background()
	database, err := relational.OpenSQLite(ctx, filepath.Join(t.TempDir(), "media-http.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer database.Close()
	if err := database.InitializeSchema(ctx); err != nil {
		t.Fatal(err)
	}
	objects, err := localmedia.NewLocalStore(filepath.Join(t.TempDir(), "objects"))
	if err != nil {
		t.Fatal(err)
	}
	service := mediaapp.NewService(relational.NewMediaAssetRepository(database), objects, nil, mediaapp.Config{
		PublicBaseURL: "https://api.example", MaxImageBytes: 32 << 20, MaxTotalBytes: 1 << 30,
		CleanupThresholdPercent: 80, CleanupInterval: 10 * time.Minute,
	})
	raw, _ := base64.StdEncoding.DecodeString("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
	asset, err := service.SaveImage(ctx, raw)
	if err != nil {
		t.Fatal(err)
	}
	router := gin.New()
	NewHandler(service, "").RegisterPublic(router)
	path := "/v1/media/images/" + asset.ID

	get := httptest.NewRecorder()
	router.ServeHTTP(get, httptest.NewRequest(http.MethodGet, path, nil))
	if get.Code != http.StatusOK || get.Header().Get("Content-Type") != "image/png" || get.Body.Len() != len(raw) || get.Header().Get("ETag") == "" {
		t.Fatalf("GET status=%d headers=%#v size=%d", get.Code, get.Header(), get.Body.Len())
	}
	head := httptest.NewRecorder()
	router.ServeHTTP(head, httptest.NewRequest(http.MethodHead, path, nil))
	if head.Code != http.StatusOK || head.Body.Len() != 0 || head.Header().Get("Content-Length") == "" {
		t.Fatalf("HEAD status=%d headers=%#v size=%d", head.Code, head.Header(), head.Body.Len())
	}
	notModifiedRequest := httptest.NewRequest(http.MethodGet, path, nil)
	notModifiedRequest.Header.Set("If-None-Match", get.Header().Get("ETag"))
	notModified := httptest.NewRecorder()
	router.ServeHTTP(notModified, notModifiedRequest)
	if notModified.Code != http.StatusNotModified || notModified.Body.Len() != 0 {
		t.Fatalf("conditional GET status=%d size=%d", notModified.Code, notModified.Body.Len())
	}
}

func TestLegacyMediaCompatibilityRoutes(t *testing.T) {
	gin.SetMode(gin.TestMode)
	root := t.TempDir()
	if err := os.MkdirAll(filepath.Join(root, "images"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(root, "videos"), 0o700); err != nil {
		t.Fatal(err)
	}
	id := "01234567-89ab-cdef"
	image := []byte("legacy-image")
	video := []byte("legacy-video")
	if err := os.WriteFile(filepath.Join(root, "images", id+".png"), image, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "videos", id+".mp4"), video, 0o600); err != nil {
		t.Fatal(err)
	}
	router := gin.New()
	NewHandler(nil, root).RegisterPublic(router)

	for _, test := range []struct {
		path        string
		contentType string
		body        []byte
	}{
		{path: "/v1/files/image?id=" + id, contentType: "image/png", body: image},
		{path: "/v1/files/video?id=" + id, contentType: "video/mp4", body: video},
	} {
		response := httptest.NewRecorder()
		router.ServeHTTP(response, httptest.NewRequest(http.MethodGet, test.path, nil))
		if response.Code != http.StatusOK || response.Header().Get("Content-Type") != test.contentType || !bytes.Equal(response.Body.Bytes(), test.body) {
			t.Fatalf("GET %s status=%d content-type=%q body=%q", test.path, response.Code, response.Header().Get("Content-Type"), response.Body.Bytes())
		}
		head := httptest.NewRecorder()
		router.ServeHTTP(head, httptest.NewRequest(http.MethodHead, test.path, nil))
		if head.Code != http.StatusOK || head.Body.Len() != 0 {
			t.Fatalf("HEAD %s status=%d size=%d", test.path, head.Code, head.Body.Len())
		}
	}

	invalid := httptest.NewRecorder()
	router.ServeHTTP(invalid, httptest.NewRequest(http.MethodGet, "/v1/files/image?id=../../secret", nil))
	if invalid.Code != http.StatusBadRequest {
		t.Fatalf("invalid id status=%d", invalid.Code)
	}
	missing := httptest.NewRecorder()
	router.ServeHTTP(missing, httptest.NewRequest(http.MethodGet, "/v1/files/video?id=ffffffffffffffff", nil))
	if missing.Code != http.StatusNotFound {
		t.Fatalf("missing media status=%d", missing.Code)
	}
}
