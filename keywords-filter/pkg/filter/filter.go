package filter

import (
	"bufio"
	"github.com/importcjj/sensitive"
	"keywords-filter/pkg/log"
	"os"
	"regexp"
	"strings"
)

type IFilter interface {
	Validate(text string) (bool, string)
	FindAll(text string) []string
}

type filter struct {
	filter *sensitive.Filter
}

func (f *filter) Validate(text string) (bool, string) {
	text = " " + strings.Trim(text, " ") + " "
	ok, word := f.filter.Validate(text)
	return ok, word
}
func (f *filter) FindAll(text string) []string {
	text = " " + strings.Trim(text, " ") + " "
	list := f.filter.FindAll(text)
	for i := 0; i < len(list); i++ {
		list[i] = strings.Trim(list[i], " ")
	}
	return list
}

var _filter *filter

func GetFilter() IFilter {
	return _filter
}

func InitFilter(dictFilePath string) {
	if dictFilePath == "" {
		log.Fatal("请指定词库文件")
	}
	_, err := os.Stat(dictFilePath)
	if os.IsNotExist(err) {
		log.Fatal("词库文件不存在，请指定正确的词库文件")
	}
	f := sensitive.New()
	f.UpdateNoisePattern("")
	f.LoadWordDict(dictFilePath)
	_filter = &filter{
		filter: f,
	}
}

func OverwriteDict(dictFilePath string) error {
	file, err := os.Open(dictFilePath)
	if err != nil {
		log.Fatal(err)
		return err
	}
	re := regexp.MustCompile(`\p{Han}+`)
	newContent := ""
	kwMp := make(map[string]struct{}, 0)

	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		line := scanner.Text()
		line = strings.Trim(line, " ")
		//去重
		if _, ok := kwMp[line]; ok {
			continue
		}
		kwMp[line] = struct{}{}
		match := re.FindString(line)
		if match == "" {
			newContent += " " + line + " \n"
		} else {
			newContent += line + "\n"
		}
	}
	newContent = strings.Trim(newContent, "\n")
	file.Close()
	file, err = os.OpenFile(dictFilePath, os.O_TRUNC|os.O_CREATE|os.O_WRONLY, 0644)
	defer file.Close()
	if err != nil {
		log.Fatal(err)
		return err
	}
	_, err = file.WriteString(newContent)
	if err != nil {
		log.Fatal(err)
		return err
	}
	return err

}
