import axios, { type AxiosResponse } from 'axios'
import { ss } from '@/utils/storage'

const service = axios.create({
  baseURL: import.meta.env.VITE_GLOB_API_URL,
})

service.interceptors.request.use(
  (config) => {
    const access_token = ss.get('SECRET_TOKEN')
    if (access_token)
      config.headers.Authorization = access_token
    return config
  },
  (error) => {
    return Promise.reject(error.response)
  },
)

service.interceptors.response.use(
  (response: AxiosResponse): AxiosResponse => {
    if (response.status === 200)
      return response

    if (response.status === 401) {
      ss.remove('SECRET_TOKEN')
      window.location.reload()
    }

    throw new Error(response.status.toString())
  },
  (error) => {
    return Promise.reject(error)
  },
)

export default service
