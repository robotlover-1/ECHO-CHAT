package users

import (
	"database/sql"
	"errors"

	db "ai-chat-backend/pkg/db/mysql"
)

type User struct {
	ID       int64
	DeviceID string
	Quota    int
}

func GetByDeviceID(deviceID string) (*User, error) {
	u := &User{}
	err := db.GetDB().QueryRow(
		"SELECT id, device_id, quota FROM users WHERE device_id = ?", deviceID,
	).Scan(&u.ID, &u.DeviceID, &u.Quota)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return u, nil
}

// UpsertByDeviceID: 已存在返回现有行；不存在插入新用户（initQuota 初始额度）
func UpsertByDeviceID(deviceID string, initQuota int) (*User, error) {
	u, err := GetByDeviceID(deviceID)
	if err != nil {
		return nil, err
	}
	if u != nil {
		return u, nil
	}
	if _, err := db.GetDB().Exec(
		"INSERT INTO users (device_id, quota) VALUES (?, ?)", deviceID, initQuota,
	); err != nil {
		return nil, err
	}
	return GetByDeviceID(deviceID)
}

func DeductQuota(deviceID string, tokens int) error {
	if tokens <= 0 {
		return nil
	}
	_, err := db.GetDB().Exec(
		"UPDATE users SET quota = GREATEST(0, quota - ?) WHERE device_id = ?", tokens, deviceID,
	)
	return err
}
