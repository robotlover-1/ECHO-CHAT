package mysql

import (
	"database/sql"
	"fmt"
	"sync"

	"ai-chat-backend/pkg/config"

	_ "github.com/go-sql-driver/mysql"
)

var (
	db   *sql.DB
	once sync.Once
)

func GetDB() *sql.DB {
	once.Do(func() {
		cnf := config.GetConfig()
		var err error
		db, err = sql.Open("mysql", cnf.Mysql.DSN)
		if err != nil {
			panic(fmt.Sprintf("mysql open: %v", err))
		}
		db.SetMaxOpenConns(cnf.Mysql.MaxOpenConn)
		db.SetMaxIdleConns(cnf.Mysql.MaxIdleConn)
	})
	return db
}

func InitUsersTable() error {
	_, err := GetDB().Exec(`CREATE TABLE IF NOT EXISTS users (
		id INT AUTO_INCREMENT PRIMARY KEY,
		device_id VARCHAR(64) NOT NULL UNIQUE,
		quota INT NOT NULL DEFAULT 100000,
		created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
	)`)
	return err
}
