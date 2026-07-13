package media

import (
	"errors"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"

	mediaapp "github.com/chenyme/grok2api/backend/internal/application/media"
	"github.com/gin-gonic/gin"
)

type Handler struct {
	service    *mediaapp.Service
	legacyRoot string
}

var legacyFileIDPattern = regexp.MustCompile(`^[0-9a-f-]{16,36}$`)

func NewHandler(service *mediaapp.Service, legacyRoot string) *Handler {
	return &Handler{service: service, legacyRoot: strings.TrimSpace(legacyRoot)}
}

// RegisterPublic 注册使用不可猜测资源 ID 的公开图片读取端点。
func (h *Handler) RegisterPublic(router *gin.Engine) {
	router.GET("/v1/media/images/:assetId", h.getImage)
	router.HEAD("/v1/media/images/:assetId", h.getImage)
	if h.legacyRoot != "" {
		router.GET("/v1/files/image", h.getLegacyImage)
		router.HEAD("/v1/files/image", h.getLegacyImage)
		router.GET("/v1/files/video", h.getLegacyVideo)
		router.HEAD("/v1/files/video", h.getLegacyVideo)
	}
}

func (h *Handler) getImage(c *gin.Context) {
	asset, body, err := h.service.OpenImage(c.Request.Context(), c.Param("assetId"))
	if errors.Is(err, mediaapp.ErrAssetNotFound) {
		c.Status(http.StatusNotFound)
		return
	}
	if err != nil {
		c.Status(http.StatusInternalServerError)
		return
	}
	defer body.Close()
	etag := `"` + asset.SHA256 + `"`
	if strings.TrimSpace(c.GetHeader("If-None-Match")) == etag {
		c.Header("ETag", etag)
		c.Status(http.StatusNotModified)
		return
	}
	c.Header("Content-Type", asset.MIMEType)
	c.Header("Content-Length", strconv.FormatInt(asset.SizeBytes, 10))
	c.Header("Cache-Control", "public, max-age=31536000, immutable")
	c.Header("ETag", etag)
	c.Header("X-Content-Type-Options", "nosniff")
	if c.Request.Method == http.MethodHead {
		c.Status(http.StatusOK)
		return
	}
	c.Status(http.StatusOK)
	_, _ = io.Copy(c.Writer, body)
}

func (h *Handler) getLegacyImage(c *gin.Context) {
	id, ok := legacyFileID(c)
	if !ok {
		return
	}
	for _, candidate := range []struct {
		extension string
		mimeType  string
	}{
		{extension: ".jpg", mimeType: "image/jpeg"},
		{extension: ".png", mimeType: "image/png"},
	} {
		path := filepath.Join(h.legacyRoot, "images", id+candidate.extension)
		if serveLegacyFile(c, path, candidate.mimeType) {
			return
		}
	}
	c.Status(http.StatusNotFound)
}

func (h *Handler) getLegacyVideo(c *gin.Context) {
	id, ok := legacyFileID(c)
	if !ok {
		return
	}
	if !serveLegacyFile(c, filepath.Join(h.legacyRoot, "videos", id+".mp4"), "video/mp4") {
		c.Status(http.StatusNotFound)
	}
}

func legacyFileID(c *gin.Context) (string, bool) {
	id := strings.TrimSpace(c.Query("id"))
	if !legacyFileIDPattern.MatchString(id) {
		c.Status(http.StatusBadRequest)
		return "", false
	}
	return id, true
}

func serveLegacyFile(c *gin.Context, path, mimeType string) bool {
	file, err := os.Open(path)
	if errors.Is(err, os.ErrNotExist) {
		return false
	}
	if err != nil {
		c.Status(http.StatusInternalServerError)
		return true
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil || !info.Mode().IsRegular() {
		c.Status(http.StatusInternalServerError)
		return true
	}
	c.Header("Content-Type", mimeType)
	c.Header("Cache-Control", "public, max-age=31536000, immutable")
	c.Header("X-Content-Type-Options", "nosniff")
	http.ServeContent(c.Writer, c.Request, info.Name(), info.ModTime(), file)
	return true
}
